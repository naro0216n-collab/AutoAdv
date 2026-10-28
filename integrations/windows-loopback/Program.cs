using System.Buffers.Binary;
using System.Diagnostics;
using NAudio.CoreAudioApi;
using NAudio.Wave;

// Dedicated process, outside the target game's PID tree. stdout is a binary
// protocol (the 16-byte fixed header + PCM); stderr is diagnostic only.
// Header: 4-byte little-endian payload length, 8-byte monotonic milliseconds,
// 4-byte format code (1=float32 LE, 0=health heartbeat). PCM is 48kHz stereo.
// For PCM packets the timestamp is the captured-frame end, derived from the
// WASAPI packet QPC position rather than callback scheduling time.
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

static long QpcMilliseconds()
    => (long)Math.Round(Stopwatch.GetTimestamp() * 1000.0 / Stopwatch.Frequency);

long lastWrite = QpcMilliseconds();
void Emit(ReadOnlySpan<byte> data, uint format, long packetQpc100ns = 0)
{
    long timestamp;
    if (format == 1 && packetQpc100ns > 0)
    {
        // IAudioCaptureClient reports QPC at the packet start in 100 ns units.
        // The protocol historically carries a packet-end timestamp, so preserve
        // that contract by advancing exactly by the captured frame duration.
        long frames = data.Length / (2 * sizeof(float));
        double startMs = packetQpc100ns / 10_000.0;
        timestamp = (long)Math.Round(startMs + frames * 1000.0 / 48_000.0);
    }
    else
    {
        // Health packets, and the defensive fallback for a driver that omits
        // QPC position, use the same QPC-derived monotonic clock domain.
        timestamp = QpcMilliseconds();
    }

    Span<byte> header = stackalloc byte[16];
    BinaryPrimitives.WriteUInt32LittleEndian(header[..4], (uint)data.Length);
    BinaryPrimitives.WriteInt64LittleEndian(header[4..12], timestamp);
    BinaryPrimitives.WriteUInt32LittleEndian(header[12..16], format);
    lock (gate)
    {
        output.Write(header);
        if (!data.IsEmpty) output.Write(data);
        output.Flush();
        lastWrite = QpcMilliseconds();
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
    recorder.DataAvailable += (buffer, _, _, qpcPosition) => Emit(buffer, 1, qpcPosition);
    recorder.StartRecording();
    // Establish the helper/controller clock mapping immediately even when the
    // process has not rendered audio yet. This keeps delayed first-audio packets
    // from defining the clock offset through their pipe delivery latency.
    Emit(ReadOnlySpan<byte>.Empty, 0);
    while (!cts.Token.IsCancellationRequested)
    {
        await Task.Delay(100, cts.Token);
        if (QpcMilliseconds() - lastWrite >= 100)
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
