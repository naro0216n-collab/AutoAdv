using System.Buffers.Binary;
using NAudio.CoreAudioApi;
using NAudio.Wave;

// Dedicated process, outside the target game's PID tree. stdout is a binary
// protocol (the 16-byte fixed header + PCM); stderr is diagnostic only.
// Header: 4-byte little-endian payload length, 8-byte monotonic milliseconds,
// 4-byte format code (1=float32 LE, 0=health heartbeat). PCM is 48kHz stereo.
if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 19041) ||
    args.Length != 2 || args[0] != "--pid" ||
    !uint.TryParse(args[1], out uint pid) || pid == 0)
{
    Console.Error.WriteLine("Requires Windows 10 2004+ and --pid <game process ID>.");
    return 2;
}

using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
var output = Console.OpenStandardOutput();
var gate = new object();
long lastWrite = Environment.TickCount64;
void Emit(ReadOnlySpan<byte> data, uint format)
{
    Span<byte> header = stackalloc byte[16];
    BinaryPrimitives.WriteUInt32LittleEndian(header[..4], (uint)data.Length);
    BinaryPrimitives.WriteInt64LittleEndian(header[4..12], Environment.TickCount64);
    BinaryPrimitives.WriteUInt32LittleEndian(header[12..16], format);
    lock (gate)
    {
        output.Write(header);
        if (!data.IsEmpty) output.Write(data);
        output.Flush();
        lastWrite = Environment.TickCount64;
    }
}
try
{
    // AudioClient activation returns a virtual process-loopback endpoint,
    // never the system-wide mixed render endpoint.
    await using var recorder = await new WasapiRecorderBuilder()
        .WithProcessLoopback(pid, ProcessLoopbackMode.IncludeTargetProcessTree)
        .WithFormat(WaveFormat.CreateIeeeFloatWaveFormat(48000, 2))
        .BuildAsync();
    recorder.DataAvailable += (buffer, _, _, _) => Emit(buffer, 1);
    recorder.StartRecording();
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(100, cts.Token);
        if (Environment.TickCount64 - lastWrite >= 100)
            Emit(ReadOnlySpan<byte>.Empty, 0);
    }
}
catch (OperationCanceledException) { }
catch (Exception ex)
{
    Console.Error.WriteLine(ex);
    return 1;
}
return 0;
