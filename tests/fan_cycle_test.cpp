#include "../plotter_firmware/FanCycle.h"
#include <cassert>

int main() {
  FanCycle fan;
  fan.complete();
  assert(fan.count == 0 && !fan.on);
  fan.begin();
  for (uint32_t i = 1; i <= 600000; ++i) {
    fan.complete();
    assert(fan.count == i);
    assert(fan.on == (i % 5000 >= 2000));
  }
  fan.begin();
  for (int i = 0; i < 2000; ++i) fan.complete();
  assert(fan.on);
  fan.setManual(false);
  for (int i = 0; i < 1000; ++i) fan.complete();
  assert(!fan.on && fan.count == 3000);
  fan.setAutomatic();
  assert(fan.on); // resumes the current plot phase
  fan.end();
  assert(!fan.on && !fan.active);
  fan.setManual(true);
  fan.begin();
  assert(fan.on && fan.count == 0);
  for (int i = 0; i < 5000; ++i) fan.complete();
  fan.end();
  assert(fan.on); // manual cooling survives a completed or stopped plot
  fan.setAutomatic();
  assert(!fan.on);
  fan.begin();
  assert(fan.phase == 0 && fan.count == 0 && !fan.on);
}
