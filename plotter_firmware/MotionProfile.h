#ifndef CITADELA_MOTION_PROFILE_H
#define CITADELA_MOTION_PROFILE_H

#include <stdint.h>
#include <math.h>

// Geometry and easing are shared by the Uno implementation and native tests.
// Floating point is used only when a command is admitted, never for a pulse.
inline uint16_t motionJunction(int32_t ax, int32_t ay, int32_t bx, int32_t by,
                               uint16_t aFeed, uint16_t bFeed,
                               uint16_t start, uint8_t cornerPercent) {
  uint16_t limit = aFeed < bFeed ? aFeed : bFeed;
  uint16_t floor = start < limit ? start : limit;
  if ((!ax && !ay) || (!bx && !by)) return limit;
  float dot = (float)ax * bx + (float)ay * by;
  if (dot <= 0) return floor;
  bool reverses = (ax && bx && ((ax < 0) != (bx < 0))) ||
                  (ay && by && ((ay < 0) != (by < 0)));
  float cross = (float)ax * by - (float)ay * bx;
  // Within about 18 degrees the speed is carried across the segment boundary.
  // A sign change on either axis is treated as a corner even at a small angle.
  if (!reverses && cross * cross <= dot * dot * 0.105263158f) return limit;
  uint16_t corner = (uint32_t)limit * cornerPercent / 100;
  return corner > floor ? corner : floor;
}

struct MotionDDA {
  volatile int32_t x = 0, y = 0;
  int32_t dx = 0, dy = 0, major = 0, xError = 0, yError = 0;
  volatile int32_t remaining = 0;
  int8_t xSign = 1, ySign = 1;
  bool xEvery = false, yEvery = false, stepX = false, stepY = false;

  void begin(int32_t targetX, int32_t targetY) {
    int32_t signedX = targetX - x, signedY = targetY - y;
    xSign = signedX >= 0 ? 1 : -1;
    ySign = signedY >= 0 ? 1 : -1;
    dx = signedX >= 0 ? signedX : -signedX;
    dy = signedY >= 0 ? signedY : -signedY;
    major = dx > dy ? dx : dy;
    remaining = major;
    xError = yError = 0;
    xEvery = dx == major && dx;
    yEvery = dy == major && dy;
    prepare();
  }

  void prepare() {
    stepX = xEvery;
    stepY = yEvery;
    if (!xEvery && dx) {
      xError += dx;
      if (xError >= major) { xError -= major; stepX = true; }
    }
    if (!yEvery && dy) {
      yError += dy;
      if (yError >= major) { yError -= major; stepY = true; }
    }
  }

  void pulse() {
    if (stepX) x += xSign;
    if (stepY) y += ySign;
    --remaining;
  }

  void stop() { remaining = 0; stepX = stepY = false; }
};

struct MotionInterval {
  uint32_t ticks = 2500;
  uint16_t feed = 800, remainder = 0, carry = 0;
  uint8_t fastTicks = 0;

  void set(uint16_t rate) {
    feed = rate;
    ticks = 2000000UL / rate; // Timer2 /8: one tick is 0.5 microseconds.
    remainder = 2000000UL % rate;
    fastTicks = ticks <= 254 ? ticks : 0;
    carry = 0;
  }

  uint32_t next() {
    uint32_t result = ticks;
    uint16_t nextCarry = carry + remainder;
    if (nextCarry >= feed) { ++result; nextCarry -= feed; }
    carry = nextCarry;
    return result;
  }
};

struct MotionRamp {
  uint16_t start = 800, duration = 300;
  uint8_t corner = 25;
  uint16_t rate = 800, from = 800, to = 800;
  uint32_t since = 0;
  bool braking = false;

  void configure(uint16_t initial, uint16_t milliseconds, uint8_t percent) {
    start = initial; duration = milliseconds; corner = percent;
  }

  void reset(uint16_t actual, uint32_t now) {
    rate = from = to = actual;
    since = now;
    braking = false;
  }

  void segment(uint16_t actual, uint32_t now) {
    // Preserve an ongoing acceleration through compatible short segments.
    if (actual != rate) reset(actual, now);
    braking = false;
  }

  uint16_t value(uint32_t now) const {
    uint32_t elapsed = now - since; // unsigned subtraction handles millis wrap.
    if (elapsed >= duration) return to;
    uint32_t phase = elapsed * 65536UL / duration;
    uint32_t square = (phase * phase) >> 16;
    uint32_t eased = (square * (196608UL - 2 * phase)) >> 16;
    int32_t delta = (int32_t)to - from;
    return from + delta * (int32_t)eased / 65536L;
  }

  uint16_t update(uint32_t now, uint32_t stepsToJunction,
                  uint16_t feed, uint16_t junction) {
    rate = value(now);
    if (junction > feed) junction = feed;
    if (junction >= feed) braking = false;
    // Reserve two 4-ms intervals: one for the next controller sample and one
    // for the extra distance from holding each deceleration sample's rate.
    uint32_t brakingSteps = ((uint32_t)rate + junction) * duration / 2000UL +
                            (uint32_t)feed * 8 / 1000 + 2;
    if (junction < feed && stepsToJunction <= brakingSteps) braking = true;
    uint16_t desired = braking ? junction : feed;
    if (desired != to) { from = rate; to = desired; since = now; }
    rate = value(now);
    return rate;
  }
};

#endif
