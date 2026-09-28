#pragma once
#include <stdint.h>

// Counts executed plot commands, never serial queries or queue admissions.
// No division/modulo on the motion completion path.
class FanCycle {
 public:
  bool automatic = true;
  bool on = false;
  bool active = false;
  uint32_t count = 0;
  uint16_t phase = 0;

  void begin() {
    active = true;
    count = 0;
    phase = 0;
    applyAutomatic();
  }
  void end() {
    active = false;
    applyAutomatic();
  }
  void setAutomatic() {
    automatic = true;
    applyAutomatic();
  }
  void setManual(bool requested) {
    automatic = false;
    on = requested;
  }
  void complete() {
    if (!active) return;
    ++count;
    if (++phase == 5000) phase = 0;
    applyAutomatic();
  }

 private:
  void applyAutomatic() {
    if (automatic) on = active && phase >= 2000;
  }
};
