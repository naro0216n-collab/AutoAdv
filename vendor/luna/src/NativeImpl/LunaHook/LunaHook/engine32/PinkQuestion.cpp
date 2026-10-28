#include "PinkQuestion.h"

// ぴんくはてな\魔法少女ナユタ
bool PinkQuestion::attach_function()
{
  BYTE bytes2[] = {
      0x3d, 0x9f, 0x82, 0x00, 0x00,
      0xbd, 0x02, 0x00, 0x00, 0x00,
      0x7c, XX,
      0x3d, 0xf1, 0x82, 0x00, 0x00,
      0x7f, XX,
      0x33, 0xed,
      0x3d, 0x41, 0x81, 0x00, 0x00,
      0x7c, XX,
      0x3d, 0x9a, 0x82, 0x00, 0x00,
      0x7e, XX,
      0x3d, 0x40, 0x83, 0x00, 0x00,
      0x7c, XX,
      0x3d, 0x8f, 0x87, 0x00, 0x00,
      0x7f, XX};
  auto addr = MemDbg::findBytes(bytes2, sizeof(bytes2), processStartAddress, processStopAddress);
  if (!addr)
    return false;
  addr = MemDbg::findEnclosingAlignedFunction(addr, 0x20);
  if (!addr)
    return false;
  auto addrs = findxref_reverse_checkcallop(addr, processStartAddress, processStopAddress, 0xe8);
  if (addrs.size() != 1)
    return false;
  addr = MemDbg::findEnclosingAlignedFunction(addrs[0], 0x40);
  if (!addr)
    return false;
  HookParam hp;
  hp.address = addr;
  hp.type = USING_CHAR | DATA_INDIRECT;
  hp.offset = stackoffset(3);
  return NewHook(hp, "PinkQuestion");
}