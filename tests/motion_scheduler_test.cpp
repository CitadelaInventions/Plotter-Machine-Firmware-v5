#include <cassert>
#include <iostream>
#include <vector>
#include <algorithm>
#include "native_motion_stubs/Arduino.h"

uint8_t PORTD = 0, PORTC = 0, TCCR2A = 0, TCCR2B = 0, TIMSK2 = 0;
uint8_t TIFR2 = 0, OCR2A = 0, TCNT2 = 0;
uint64_t fakeTicks = 0;
int fakePins[20] = {};
NativeSerial Serial;

// Include the actual queue, STOP, ramp controller and timer ISR. Native stubs
// model timer events; ISR execution cost itself is reviewed in the AVR build.
#include "../plotter_firmware/plotter_firmware.ino"

void command(const std::string &text) {
  std::vector<char> line(text.begin(), text.end()); line.push_back(0);
  handle(line.data());
}

void fresh(unsigned int initial = 800, unsigned int duration = 300) {
  stopMotion(false);
  fakeTicks = 0; TCNT2 = 0; PORTD = PORTC = 0;
  xPosition = yPosition = 0; xLimit = yLimit = MAX_COORDINATE;
  completedInstructions = 0; legacyDone = false;
  segmentVersion = 0; lastRampUpdate = 0;
  ramp.configure(initial, duration, 25); ramp.reset(initial, 0);
  Serial.output.clear();
  enableDrivers(); lastContact = millis();
}

struct Observed {
  unsigned int peak = 0;
  uint64_t previousRise = 0, previousFall = 0, directionAt = 0;
  bool high = false;
  byte direction = 0;
  unsigned long pulses = 0;
  std::vector<unsigned long> riseIntervals;
  std::vector<unsigned int> boundaryRates;
};

void timerEvent(Observed &seen) {
  assert(TIMSK2 & _BV(OCIE2A));
  unsigned int delta = (byte)(OCR2A - TCNT2);
  if (!delta) delta = 256;
  fakeTicks += delta; TCNT2 = OCR2A;
  bool wasHigh = (PORTD & _BV(PD2)) || (PORTC & _BV(PC0));
  byte beforeVersion = segmentVersion;
  unsigned int beforeRate = currentFeed;
  TIMER2_COMPA_vect();
  bool isHigh = (PORTD & _BV(PD2)) || (PORTC & _BV(PC0));
  byte direction = (PORTD & _BV(PD3)) | ((PORTC & _BV(PC1)) << 4);
  if (direction != seen.direction) { seen.direction = direction; seen.directionAt = fakeTicks; }
  if (!wasHigh && isHigh) {
    if (seen.pulses) {
      assert(fakeTicks - seen.previousFall >= 6); // at least 3 us LOW.
      seen.riseIntervals.push_back(fakeTicks - seen.previousRise);
    }
    assert(fakeTicks - seen.directionAt >= 10); // DM556 DIR setup >=5 us.
    seen.previousRise = fakeTicks; ++seen.pulses;
  }
  if (wasHigh && !isHigh) {
    assert(fakeTicks - seen.previousRise >= 20); // at least 10 us HIGH.
    seen.previousFall = fakeTicks;
  }
  if (segmentVersion != beforeVersion) seen.boundaryRates.push_back(beforeRate);
  seen.peak = std::max(seen.peak, (unsigned int)currentFeed);
  // Foreground runs between interrupts. Keepalive/report paths also execute.
  lastContact = millis();
  serviceMotionReports(); sendCompleted(); updateRamp();
  if (!moving && queueCount && completedCount < COMPLETION_CAPACITY) startNextQueued();
}

Observed finish() {
  Observed seen;
  seen.direction = (PORTD & _BV(PD3)) | ((PORTC & _BV(PC1)) << 4);
  seen.directionAt = fakeTicks;
  unsigned long guard = 0;
  while (moving || queueCount) {
    assert(++guard < 2000000UL);
    if (!(TIMSK2 & _BV(OCIE2A))) startNextQueued();
    timerEvent(seen);
  }
  serviceMotionReports(); sendCompleted();
  return seen;
}

void exactEndpointsAndPulseWidths() {
  fresh(MAX_FEED);
  command("MOVE 9999 4321 " + std::to_string(MAX_FEED));
  Observed seen = finish();
  assert(xPosition == 9999 && yPosition == 4321 && seen.pulses == 9999);
  assert(!moving && !(TIMSK2 & _BV(OCIE2A)));
  assert(Serial.output.find("DONE\n") != std::string::npos);
  unsigned long smallest = 2000000UL / MAX_FEED;
  for (unsigned long interval : seen.riseIntervals)
    assert(interval == smallest || interval == smallest + 1);
  unsigned long total = 0;
  for (unsigned long interval : seen.riseIntervals) total += interval;
  unsigned long expected = seen.riseIntervals.size() * 2000000UL / MAX_FEED;
  assert(total == expected || total == expected + 1);
}

void easingAndQueuedContinuity() {
  fresh();
  for (unsigned int id = 1; id <= 31; ++id)
    command("QUEUE " + std::to_string(id) + " " + std::to_string(id * 400) + " 0 " + std::to_string(MAX_FEED));
  Observed seen = finish();
  assert(xPosition == 12400 && yPosition == 0 && seen.pulses == 12400);
  assert(seen.peak >= MAX_FEED * 95 / 100); // ramp survives short segment boundaries.
  assert(seen.boundaryRates.size() == 30);
  assert(*std::max_element(seen.boundaryRates.begin(), seen.boundaryRates.end()) > 10000);
  assert(Serial.output.find("DONE 31\n") != std::string::npos);
}

void reversalBrakesBeforeDirectionChange() {
  fresh();
  for (unsigned int id = 1; id <= 30; ++id)
    command("QUEUE " + std::to_string(id) + " " + std::to_string(id * 400) + " 0 " + std::to_string(MAX_FEED));
  command("QUEUE 31 11600 0 " + std::to_string(MAX_FEED));
  Observed seen = finish();
  assert(xPosition == 11600 && yPosition == 0 && seen.pulses == 12400);
  if (seen.boundaryRates.back() > 810)
    std::cerr << "Rate before reversal=" << seen.boundaryRates.back() << " pps\n";
  assert(seen.boundaryRates.back() <= 810);
}

void stopCannotIssueAnotherPulse() {
  fresh(); command("QUEUE 1 5000 3000 " + std::to_string(MAX_FEED));
  command("QUEUE 2 6000 4000 " + std::to_string(MAX_FEED));
  Observed seen; seen.directionAt = fakeTicks;
  while (seen.pulses < 20) timerEvent(seen);
  long stoppedX = xPosition, stoppedY = yPosition;
  command("STOP");
  assert(!moving && !queueCount && !completedCount && !enabled);
  for (int i = 0; i < 20; ++i) TIMER2_COMPA_vect();
  assert(xPosition == stoppedX && yPosition == stoppedY);
  assert(!(PORTD & _BV(PD2)) && !(PORTC & _BV(PC0)));
}

void intervalAndTimeWrap() {
  MotionInterval interval; interval.set(32000);
  uint32_t clock = UINT32_MAX - 17, total = 0;
  for (int i = 0; i < 32000; ++i) { uint32_t ticks = interval.next(); clock += ticks; total += ticks; }
  assert(total == 2000000UL && clock == (uint32_t)(UINT32_MAX - 17 + 2000000UL));
  MotionRamp easing; easing.configure(800, 300, 25); easing.reset(800, UINT32_MAX - 100);
  easing.update(UINT32_MAX - 100, 100000, 32000, 32000);
  assert(easing.value(49) > 16000 && easing.value(49) < 17000);
  assert(easing.value(199) == 32000);
  uint16_t prior = 800;
  for (uint32_t elapsed = 0; elapsed <= 300; ++elapsed) {
    uint16_t rate = easing.value((uint32_t)(UINT32_MAX - 100 + elapsed));
    assert(rate >= prior && rate >= 1 && rate <= 32000); prior = rate;
  }
}

void slowMovesAndLargeCalibrationBounds() {
  fresh(1); command("MOVE 3 2 1");
  Observed seen = finish();
  assert(seen.pulses == 3 && xPosition == 3 && yPosition == 2);
  assert(fakeTicks >= 6000000UL); // long Timer2 chunks preserve one pulse/sec.
  fresh(); command("LIMIT 20000000 20000000");
  command("MOVE 20000000 20000000 " + std::to_string(MAX_FEED));
  assert(moving && xTarget == 20000000L && yTarget == 20000000L);
  command("STOP");
  Serial.output.clear(); command("RAMP 800 49 25");
  assert(Serial.output.find("ERR invalid_ramp") != std::string::npos);
}

int main() {
  setup();
  exactEndpointsAndPulseWidths(); easingAndQueuedContinuity();
  reversalBrakesBeforeDirectionChange(); stopCannotIssueAnotherPulse();
  intervalAndTimeWrap(); slowMovesAndLargeCalibrationBounds();
  std::cout << "Motion scheduler, easing, queue, reversal, STOP, pulse-width and wrap tests passed\n";
}
