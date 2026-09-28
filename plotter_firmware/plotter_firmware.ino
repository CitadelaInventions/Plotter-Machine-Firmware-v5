/*
  Four-pen XY plotter for Arduino Uno, protocol v5 at 115200 baud.

  X STEP/DIR/ENABLE: D2/D3/D10; Y STEP/DIR/ENABLE: A0/A1/A2.
  Both driver enables are active LOW.
  Red/blue/green/black servo signals: D6/D7/D8/D11.
  Cooling fan relay: D9, active HIGH (LOW is off).
  Servo library owns Timer1; XY edges use Timer2 at 0.5-microsecond ticks.

  Commands (one ASCII line each):
    STATUS, POS, STOP, ZERO, ENABLE ON|OFF
    FAN AUTO | FAN MANUAL ON|OFF; JOB BEGIN|END
    LIMIT <max_x_steps> <max_y_steps>
    RAMP <start_pps> <ramp_ms> <corner_percent> (idle only)
    SERVO <up> <red_down> <blue_down> <green_down> <black_down>
    PEN UP|RED|BLUE|GREEN|BLACK
    MOVE <absolute_x_steps> <absolute_y_steps> <feed_steps_per_second>
    JOG <delta_x_steps> <delta_y_steps> <feed_steps_per_second>
    QUEUE <id> <absolute_x_steps> <absolute_y_steps> <feed_steps_per_second>

  Commands return OK or ERR. MOVE/JOG return a later DONE or ABORT.
  QUEUE returns ACK <id> immediately and DONE <id> after its move completes.
  FAN returns a FAN report, not OK, and is allowed during queued motion.
  JOB brackets a plot: fan OFF for 2000 completed commands, ON for 3000, repeating.
  STATUS contains proto=5. Send POS or STATUS at least every 2 seconds during moves.
  Drivers start disabled and release after 5 seconds idle; STOP releases them immediately.
  Physical limit switches and an emergency power cutoff are recommended.
*/

#include <Arduino.h>
#include <Servo.h>
#include "FanCycle.h"
#include "MotionProfile.h"
#include <avr/interrupt.h>
#include <util/atomic.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

const byte X_STEP = 2, X_DIR = 3, X_ENABLE = 10;
// Uno A0-A2 also work as digital outputs and are reserved for the Y driver.
const byte Y_STEP = A0, Y_DIR = A1, Y_ENABLE = A2;
const byte SERVO_PINS[4] = {6, 7, 8, 11};
const byte FAN_PIN = 9;
FanCycle fan;
bool fanReportPending = false;
const unsigned int STEP_PULSE_US = 10;
// Worst ordinary ISR pair is 433 AVR cycles. 24 kHz reserves about 35% of
// the Uno CPU for serial, Servo, Timer0 and the 4-ms easing controller.
const long MAX_FEED = 24000;
const long MAX_COORDINATE = 20000000L;
const unsigned long HEARTBEAT_MS = 2500;
const unsigned long IDLE_RELEASE_MS = 5000;
const byte QUEUE_CAPACITY = 32;
const byte COMPLETION_CAPACITY = 16;
struct QueuedMove {
  long id;
  long x;
  long y;
  unsigned int feed;
  unsigned int junction;
  unsigned long junctionTicks;
};
QueuedMove moveQueue[QUEUE_CAPACITY];
volatile byte queueHead = 0, queueCount = 0;
volatile long activeQueueId = 0;
long completedIds[COMPLETION_CAPACITY];
volatile byte completedHead = 0, completedCount = 0;
Servo pens[4];
byte upAngle = 45;
byte downAngles[4] = {95, 95, 95, 95};
int8_t activePen = -1;
bool enabled = false;
MotionDDA motion;
#define xPosition motion.x
#define yPosition motion.y
long xLimit = 40000, yLimit = 40000;

volatile bool moving = false;
volatile long xTarget = 0, yTarget = 0;
volatile unsigned int targetFeed = 800, currentFeed = 800;
MotionInterval stepInterval;
MotionRamp ramp;
volatile byte segmentVersion = 0, completedInstructions = 0;
volatile bool legacyDone = false;
volatile bool issuedPulse = false;
volatile byte timerPhase = 0; // 0 rising edge, 1 falling edge, 2 long wait.
volatile unsigned long waitTicks = 0;
volatile byte riseDeadline = 0;
unsigned long lastContact = 0, lastRampUpdate = 0;
unsigned long lastDriverActivity = 0;
char input[80];
byte inputLength = 0;
bool overflow = false;

void __attribute__((noinline)) beginQueuedFromInterrupt();
void __attribute__((noinline)) scheduleSlowStep();

void raiseAll() {
  for (byte i = 0; i < 4; ++i) pens[i].write(upAngle);
  activePen = -1;
}

void error(const __FlashStringHelper *message) {
  Serial.print(F("ERR "));
  Serial.println(message);
}

bool parseLong(char *token, long &result) {
  if (!token || !*token) return false;
  char *end;
  result = strtol(token, &end, 10);
  return end != token && *end == '\0';
}

bool noMore(char *&save) {
  return strtok_r(NULL, " \t", &save) == NULL;
}

void applyFan() {
  digitalWrite(FAN_PIN, fan.on ? HIGH : LOW);
}

void fanReport() {
  Serial.print(F("FAN mode=")); Serial.print(fan.automatic ? F("AUTO") : F("MANUAL"));
  Serial.print(F(" on=")); Serial.print(fan.on ? 1 : 0);
  Serial.print(F(" count=")); Serial.print(fan.count);
  Serial.print(F(" active=")); Serial.println(fan.active ? 1 : 0);
  fanReportPending = false;
}

void instructionCompleted() {
  bool wasOn = fan.on;
  fan.complete();
  if (fan.on != wasOn) {
    applyFan();
    fanReportPending = true;
  }
}

void status() {
  long x, y;
  unsigned int rate;
  byte queued;
  bool active;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    x = xPosition; y = yPosition; rate = currentFeed;
    queued = queueCount; active = moving;
  }
  Serial.print(F("STATUS proto=5 x=")); Serial.print(x);
  Serial.print(F(" y=")); Serial.print(y);
  Serial.print(F(" y_pins=A0,A1,A2"));
  Serial.print(F(" max_feed=")); Serial.print(MAX_FEED);
  Serial.print(F(" accel=1"));
  Serial.print(F(" start_pps=")); Serial.print(ramp.start);
  Serial.print(F(" ramp_ms=")); Serial.print(ramp.duration);
  Serial.print(F(" corner_percent=")); Serial.print(ramp.corner);
  Serial.print(F(" current_feed=")); Serial.print(active ? rate : 0);
  Serial.print(F(" queue=")); Serial.print(QUEUE_CAPACITY);
  Serial.print(F(" idle_ms=")); Serial.print(IDLE_RELEASE_MS);
  Serial.print(F(" queued=")); Serial.print(queued);
  Serial.print(F(" moving=")); Serial.print(active ? 1 : 0);
  Serial.print(F(" enabled=")); Serial.print(enabled ? 1 : 0);
  Serial.print(F(" fan_pin=9 fan_mode=")); Serial.print(fan.automatic ? F("AUTO") : F("MANUAL"));
  Serial.print(F(" fan_on=")); Serial.print(fan.on ? 1 : 0);
  Serial.print(F(" fan_count=")); Serial.print(fan.count);
  Serial.print(F(" fan_active=")); Serial.print(fan.active ? 1 : 0);
  Serial.print(F(" pen="));
  if (activePen < 0) Serial.println(F("UP"));
  else {
    const char *names[] = {"RED", "BLUE", "GREEN", "BLACK"};
    Serial.println(names[activePen]);
  }
}

void positionReport() {
  long x, y;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { x = xPosition; y = yPosition; }
  Serial.print(F("POS ")); Serial.print(x);
  Serial.print(' '); Serial.println(y);
}

void disableDrivers() {
  digitalWrite(X_ENABLE, HIGH);
  digitalWrite(Y_ENABLE, HIGH);
  enabled = false;
}

void enableDrivers() {
  digitalWrite(X_ENABLE, LOW);
  digitalWrite(Y_ENABLE, LOW);
  enabled = true;
  delayMicroseconds(5); // DM556 ENA must precede DIR by at least 5 us.
  lastDriverActivity = millis();
}

void stopMotion(bool notify) {
  bool wasMoving;
  long abortedId;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    wasMoving = moving; abortedId = activeQueueId;
    TIMSK2 &= ~_BV(OCIE2A);
    // Finish an already issued pulse, then inhibit all subsequent edges.
    if (timerPhase == 1) delayMicroseconds(STEP_PULSE_US);
    PORTD &= ~_BV(PD2); PORTC &= ~_BV(PC0);
    moving = false; motion.stop();
    activeQueueId = 0;
    queueHead = queueCount = 0;
    completedHead = completedCount = 0;
    legacyDone = false;
  }
  if (wasMoving && notify) {
    Serial.print(F("ABORT"));
    if (abortedId) { Serial.print(' '); Serial.print(abortedId); }
    Serial.println();
  }
  raiseAll();
  disableDrivers();
  fan.end();
  applyFan();
  fanReportPending = true;
}

void setDirection() {
  if (motion.xSign > 0) PORTD |= _BV(PD3); else PORTD &= ~_BV(PD3);
  if (motion.ySign > 0) PORTC |= _BV(PC1); else PORTC &= ~_BV(PC1);
}

// Call only with interrupts disabled, or from the Timer2 ISR.
void scheduleWait(unsigned long ticks, byte minimum = 6, byte base = TCNT2) {
  if (ticks < minimum) ticks = minimum; // DM556 minimum LOW is 2.5 us.
  byte part = ticks > 255 ? 255 : (byte)ticks;
  waitTicks = ticks - part;
  timerPhase = waitTicks ? 2 : 0;
  byte now = TCNT2;
  byte late = now - base;
  OCR2A = part > late + (unsigned int)minimum ? base + part : now + minimum;
  TIFR2 = _BV(OCF2A);
  TIMSK2 |= _BV(OCIE2A);
}

void startMove(long targetX, long targetY, unsigned int feed) {
  unsigned int initial = ramp.start < feed ? ramp.start : feed;
  MotionInterval interval;
  interval.set(initial); // Division happens in foreground, before IRQ enable.
  // LTO may otherwise sink pure division into the following critical section.
  asm volatile("" : : "r" (interval.ticks), "r" (interval.remainder),
                       "r" (interval.fastTicks) : "memory");
  unsigned long now = millis();
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    xTarget = targetX; yTarget = targetY; targetFeed = feed;
    motion.begin(targetX, targetY);
    setDirection();
    stepInterval = interval;
    currentFeed = initial;
    moving = motion.remaining > 0;
    issuedPulse = false;
    ++segmentVersion;
    if (moving) scheduleWait(interval.ticks, 20); // includes DIR setup time.
  }
  ramp.reset(initial, now);
  lastDriverActivity = now;
}

// The completed-ID ring is written by the ISR and consumed by foreground.
void completeQueued(long id) {
  ++completedInstructions;
  byte tail = (completedHead + completedCount) % COMPLETION_CAPACITY;
  completedIds[tail] = id;
  ++completedCount;
}

void sendCompleted() {
  if (!completedCount || Serial.availableForWrite() < 13) return;
  long id;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (!completedCount) return;
    id = completedIds[completedHead];
    completedHead = (completedHead + 1) % COMPLETION_CAPACITY;
    --completedCount;
  }
  Serial.print(F("DONE "));
  Serial.println(id);
}

void startNextQueued() {
  while (!moving && queueCount && completedCount < COMPLETION_CAPACITY) {
    QueuedMove next;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      next = moveQueue[queueHead];
      queueHead = (queueHead + 1) % QUEUE_CAPACITY;
      --queueCount;
      activeQueueId = next.id;
    }
    startMove(next.x, next.y, next.feed);
    if (!moving) {
      ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        completeQueued(activeQueueId); activeQueueId = 0;
      }
    }
  }
}

// Continue a queued move on the falling edge, without a foreground-loop gap.
void beginQueuedFromInterrupt() {
  if (!queueCount || completedCount >= COMPLETION_CAPACITY) {
    moving = false; activeQueueId = 0;
    TIMSK2 &= ~_BV(OCIE2A);
    return;
  }
  QueuedMove &next = moveQueue[queueHead];
  activeQueueId = next.id;
  xTarget = next.x; yTarget = next.y; targetFeed = next.feed;
  if (currentFeed > next.junction) {
    currentFeed = next.junction;
    stepInterval.feed = next.junction;
    stepInterval.ticks = next.junctionTicks;
    stepInterval.fastTicks = next.junctionTicks <= 254 ? next.junctionTicks : 0;
    // Dropping the fractional remainder is conservative at a junction.
    stepInterval.remainder = stepInterval.carry = 0;
  }
  motion.begin(next.x, next.y);
  setDirection();
  queueHead = (queueHead + 1) % QUEUE_CAPACITY;
  --queueCount;
  ++segmentVersion;
  if (!motion.remaining) {
    // Zero-length commands are completed in foreground, avoiding a long ISR.
    completeQueued(activeQueueId); activeQueueId = 0; moving = false;
    TIMSK2 &= ~_BV(OCIE2A);
    return;
  }
  unsigned long period = stepInterval.next();
  byte now = TCNT2, elapsed = now - riseDeadline;
  scheduleWait(period > elapsed ? period - elapsed : 10, 10, now);
}

void scheduleSlowStep() {
  unsigned long period = stepInterval.next();
  byte now = TCNT2, elapsed = now - riseDeadline;
  scheduleWait(period > elapsed ? period - elapsed : 6, 6, now);
}

ISR(TIMER2_COMPA_vect) {
  if (!moving) { TIMSK2 &= ~_BV(OCIE2A); return; }
  if (timerPhase == 2) {
    // Long intervals are split into at most 255 timer ticks per interrupt.
    byte part = waitTicks > 255 ? 255 : (byte)waitTicks;
    waitTicks -= part;
    byte due = OCR2A + part;
    byte late = TCNT2 - OCR2A;
    if (part <= late + 6U) due = TCNT2 + 6;
    timerPhase = waitTicks ? 2 : 0;
    OCR2A = due;
    return;
  }
  if (timerPhase == 0) {
    // The next axis mask is already prepared. Keep the rising path short.
    if (motion.stepX) PORTD |= _BV(PD2);
    if (motion.stepY) PORTC |= _BV(PC0);
    riseDeadline = OCR2A;
    OCR2A = TCNT2 + STEP_PULSE_US * 2;
    timerPhase = 1;
    motion.pulse();
    issuedPulse = true;
    return;
  }
  PORTD &= ~_BV(PD2); PORTC &= ~_BV(PC0);
  if (!motion.remaining) {
    if (activeQueueId) {
      completeQueued(activeQueueId);
      beginQueuedFromInterrupt();
    } else {
      ++completedInstructions; legacyDone = true; moving = false;
      TIMSK2 &= ~_BV(OCIE2A);
    }
    return;
  }
  motion.prepare();
  if (stepInterval.fastTicks) {
    // Common high-speed path: no 32-bit wait arithmetic or IRQ re-arming.
    byte period = stepInterval.fastTicks;
    unsigned int carry = stepInterval.carry + stepInterval.remainder;
    if (carry >= stepInterval.feed) { ++period; carry -= stepInterval.feed; }
    stepInterval.carry = carry;
    byte now = TCNT2, elapsed = now - riseDeadline;
    OCR2A = period > elapsed + 6U ? riseDeadline + period : now + 6;
    timerPhase = 0;
    return;
  }
  scheduleSlowStep();
}

QueuedMove queuedCommand(long id, long x, long y, unsigned int feed) {
  long fromX, fromY, previousX, previousY;
  unsigned int previousFeed;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (queueCount) {
      byte last = (queueHead + queueCount - 1) & (QUEUE_CAPACITY - 1);
      fromX = moveQueue[last].x; fromY = moveQueue[last].y;
      previousFeed = moveQueue[last].feed;
      if (queueCount > 1) {
        byte before = (last + QUEUE_CAPACITY - 1) & (QUEUE_CAPACITY - 1);
        previousX = fromX - moveQueue[before].x;
        previousY = fromY - moveQueue[before].y;
      } else {
        previousX = fromX - (moving ? xTarget : xPosition);
        previousY = fromY - (moving ? yTarget : yPosition);
      }
    } else {
      fromX = moving ? xTarget : xPosition;
      fromY = moving ? yTarget : yPosition;
      previousX = moving ? (motion.xSign > 0 ? motion.dx : -motion.dx) : 0;
      previousY = moving ? (motion.ySign > 0 ? motion.dy : -motion.dy) : 0;
      previousFeed = moving ? targetFeed : feed;
    }
  }
  unsigned int junction = motionJunction(previousX, previousY, x - fromX, y - fromY,
                                         previousFeed, feed, ramp.start, ramp.corner);
  return {id, x, y, feed, junction, (2000000UL + junction - 1) / junction};
}

void serviceMotionReports() {
  byte count;
  bool done;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    count = completedInstructions; completedInstructions = 0;
    done = legacyDone; legacyDone = false;
  }
  if (count || done) lastDriverActivity = millis();
  while (count--) instructionCompleted();
  if (done) Serial.println(F("DONE"));
}

void updateRamp() {
  unsigned long now = millis();
  if (now - lastRampUpdate < 4 || !moving || !issuedPulse) return;
  lastRampUpdate = now;
  long targetX, targetY;
  unsigned long distance;
  unsigned int feed, rate, junction;
  byte head, count, version;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (!moving) return;
    targetX = xTarget; targetY = yTarget; distance = motion.remaining;
    feed = targetFeed; rate = currentFeed;
    head = queueHead; count = queueCount; version = segmentVersion;
  }
  static byte previousVersion = 0;
  if (version != previousVersion) {
    ramp.segment(rate, now); previousVersion = version;
  }
  junction = ramp.start < feed ? ramp.start : feed;
  // Look through compatible short segments to find the braking horizon.
  // Each small copy is atomic; the final version check rejects a stale scan.
  for (byte offset = 0; offset < count; ++offset) {
    QueuedMove next;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      if (version != segmentVersion || head != queueHead) return;
      next = moveQueue[(head + offset) % QUEUE_CAPACITY];
    }
    if (next.junction < feed) { junction = next.junction; break; }
    unsigned long nx = labs(next.x - targetX), ny = labs(next.y - targetY);
    distance += nx > ny ? nx : ny;
    targetX = next.x; targetY = next.y;
  }
  unsigned int nextRate = ramp.update(now, distance, feed, junction);
  if (nextRate == rate) return;
  MotionInterval nextInterval;
  nextInterval.set(nextRate);
  asm volatile("" : : "r" (nextInterval.ticks), "r" (nextInterval.remainder),
                       "r" (nextInterval.fastTicks) : "memory");
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (version != segmentVersion || !moving) return;
    currentFeed = nextRate;
    stepInterval.ticks = nextInterval.ticks;
    stepInterval.feed = nextInterval.feed;
    stepInterval.remainder = nextInterval.remainder;
    stepInterval.fastTicks = nextInterval.fastTicks;
    // Carry remains valid for an unchanged rate and is reset otherwise.
    if (stepInterval.carry >= nextInterval.feed) stepInterval.carry = 0;
  }
}

void handle(char *line) {
  for (char *p = line; *p; ++p) *p = (char)toupper((unsigned char)*p);
  char *save = NULL;
  char *cmd = strtok_r(line, " \t", &save);
  if (!cmd) return;
  if (!strcmp(cmd, "STATUS")) {
    if (!noMore(save)) error(F("syntax")); else status();
    return;
  }
  if (!strcmp(cmd, "POS")) {
    if (!noMore(save)) error(F("syntax")); else positionReport();
    return;
  }
  if (!strcmp(cmd, "STOP")) {
    if (!noMore(save)) { error(F("syntax")); return; }
    stopMotion(true);
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "FAN")) {
    char *mode = strtok_r(NULL, " \t", &save);
    if (mode && !strcmp(mode, "AUTO") && noMore(save)) {
      fan.setAutomatic();
    } else if (mode && !strcmp(mode, "MANUAL")) {
      char *arg = strtok_r(NULL, " \t", &save);
      if (!arg || !noMore(save) || (strcmp(arg, "ON") && strcmp(arg, "OFF"))) {
        error(F("invalid_fan")); return;
      }
      fan.setManual(!strcmp(arg, "ON"));
    } else { error(F("invalid_fan")); return; }
    applyFan();
    // Fit the whole report into the TX buffer to keep STEP scheduling responsive.
    fanReportPending = true;
    return;
  }
  if (!strcmp(cmd, "QUEUE")) {
    long id, x, y, feed;
    if (!parseLong(strtok_r(NULL, " \t", &save), id) ||
        !parseLong(strtok_r(NULL, " \t", &save), x) ||
        !parseLong(strtok_r(NULL, " \t", &save), y) ||
        !parseLong(strtok_r(NULL, " \t", &save), feed) || !noMore(save) ||
        id < 1 || id > 600000L || feed < 1 || feed > MAX_FEED ||
        x < 0 || y < 0 || x > xLimit || y > yLimit) {
      error(F("invalid_queue_move")); return;
    }
    if (!enabled) { error(F("driver_disabled")); return; }
    byte outstanding;
    bool legacyBusy;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      outstanding = queueCount + completedCount + (moving ? 1 : 0);
      legacyBusy = moving && !activeQueueId;
    }
    if (legacyBusy) { error(F("busy")); return; }
    if (outstanding >= QUEUE_CAPACITY) {
      error(F("queue_full")); return;
    }
    QueuedMove next = queuedCommand(id, x, y, (unsigned int)feed);
    asm volatile("" : : "r" (next.junction), "r" (next.junctionTicks) : "memory");
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      byte tail = (queueHead + queueCount) % QUEUE_CAPACITY;
      moveQueue[tail] = next;
      ++queueCount;
    }
    Serial.print(F("ACK ")); Serial.println(id);
    startNextQueued();
    return;
  }
  if (moving || queueCount) { error(F("busy")); return; }
  if (!strcmp(cmd, "RAMP")) {
    long initial, duration, corner;
    if (!parseLong(strtok_r(NULL, " \t", &save), initial) ||
        !parseLong(strtok_r(NULL, " \t", &save), duration) ||
        !parseLong(strtok_r(NULL, " \t", &save), corner) || !noMore(save) ||
        initial < 1 || initial > MAX_FEED || duration < 50 || duration > 3000 ||
        corner < 0 || corner > 100) { error(F("invalid_ramp")); return; }
    ramp.configure(initial, duration, corner);
    instructionCompleted();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "JOB")) {
    char *arg = strtok_r(NULL, " \t", &save);
    if (!arg || !noMore(save) || (strcmp(arg, "BEGIN") && strcmp(arg, "END"))) {
      error(F("syntax")); return;
    }
    if (!strcmp(arg, "BEGIN")) fan.begin(); else fan.end();
    applyFan();
    fanReport();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "ZERO")) {
    if (!noMore(save)) { error(F("syntax")); return; }
    xPosition = yPosition = 0;
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "ENABLE")) {
    char *arg = strtok_r(NULL, " \t", &save);
    if (!arg || !noMore(save) || (strcmp(arg, "ON") && strcmp(arg, "OFF"))) {
      error(F("syntax")); return;
    }
    if (!strcmp(arg, "ON")) enableDrivers();
    else disableDrivers();
    instructionCompleted();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "LIMIT")) {
    long x, y;
    if (!parseLong(strtok_r(NULL, " \t", &save), x) ||
        !parseLong(strtok_r(NULL, " \t", &save), y) || !noMore(save) ||
        x < 1 || y < 1 || x > MAX_COORDINATE || y > MAX_COORDINATE ||
        xPosition > x || yPosition > y) {
      error(F("invalid_limit")); return;
    }
    xLimit = x; yLimit = y;
    instructionCompleted();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "SERVO")) {
    long values[5];
    for (byte i = 0; i < 5; ++i) {
      if (!parseLong(strtok_r(NULL, " \t", &save), values[i]) ||
          values[i] < 0 || values[i] > 180) { error(F("invalid_servo")); return; }
    }
    if (!noMore(save)) { error(F("syntax")); return; }
    upAngle = (byte)values[0];
    for (byte i = 0; i < 4; ++i) downAngles[i] = (byte)values[i + 1];
    raiseAll();
    lastDriverActivity = millis();
    instructionCompleted();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "PEN")) {
    char *arg = strtok_r(NULL, " \t", &save);
    if (!arg || !noMore(save)) { error(F("syntax")); return; }
    if (!strcmp(arg, "UP")) { raiseAll(); instructionCompleted(); Serial.println(F("OK")); return; }
    const char *names[] = {"RED", "BLUE", "GREEN", "BLACK"};
    int8_t chosen = -1;
    for (byte i = 0; i < 4; ++i) if (!strcmp(arg, names[i])) chosen = i;
    if (chosen < 0) { error(F("invalid_pen")); return; }
    raiseAll();
    pens[chosen].write(downAngles[chosen]);
    activePen = chosen;
    lastDriverActivity = millis();
    instructionCompleted();
    Serial.println(F("OK"));
    return;
  }
  if (!strcmp(cmd, "MOVE") || !strcmp(cmd, "JOG")) {
    bool relative = !strcmp(cmd, "JOG");
    long x, y, feed;
    if (!parseLong(strtok_r(NULL, " \t", &save), x) ||
        !parseLong(strtok_r(NULL, " \t", &save), y) ||
        !parseLong(strtok_r(NULL, " \t", &save), feed) || !noMore(save) ||
        feed < 1 || feed > MAX_FEED) { error(F("invalid_move")); return; }
    if (!enabled) { error(F("driver_disabled")); return; }
    // Inputs are bounded before addition to avoid signed overflow on Uno.
    if (x < -MAX_COORDINATE || x > MAX_COORDINATE ||
        y < -MAX_COORDINATE || y > MAX_COORDINATE) {
      error(F("invalid_move")); return;
    }
    if (relative) { x += xPosition; y += yPosition; }
    if (x < 0 || y < 0 || x > xLimit || y > yLimit) {
      error(F("out_of_bounds")); return;
    }
    startMove(x, y, feed);
    Serial.println(F("OK"));
    if (!moving) { instructionCompleted(); Serial.println(F("DONE")); }
    return;
  }
  error(F("unknown_command"));
}

void readSerial() {
  // Bound parser work so a burst of queued commands cannot starve STEP pulses.
  byte budget = 4;
  while (budget-- && Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lastContact = millis();
      if (overflow) error(F("line_too_long"));
      else { input[inputLength] = '\0'; handle(input); }
      inputLength = 0;
      overflow = false;
    } else if (!overflow) {
      if (inputLength < sizeof(input) - 1) input[inputLength++] = c;
      else overflow = true;
    }
  }
}

void setup() {
  digitalWrite(FAN_PIN, LOW);
  pinMode(FAN_PIN, OUTPUT);
  pinMode(X_STEP, OUTPUT); pinMode(X_DIR, OUTPUT);
  pinMode(Y_STEP, OUTPUT); pinMode(Y_DIR, OUTPUT);
  digitalWrite(X_ENABLE, HIGH); digitalWrite(Y_ENABLE, HIGH);
  pinMode(X_ENABLE, OUTPUT); pinMode(Y_ENABLE, OUTPUT);
  // Timer2 normal mode, /8 prescaler: 2 MHz tick, compare A schedules both
  // edges. Timer1 remains available to all four Servo outputs.
  TCCR2A = 0;
  TCCR2B = _BV(CS21);
  TIMSK2 = 0;
  for (byte i = 0; i < 4; ++i) pens[i].attach(SERVO_PINS[i]);
  raiseAll();
  Serial.begin(115200);
  lastContact = millis();
  Serial.println(F("READY plotter_v5"));
  status();
}

void loop() {
  serviceMotionReports();
  readSerial();
  if (moving && millis() - lastContact > HEARTBEAT_MS) {
    stopMotion(true);
    error(F("host_timeout"));
  }
  if (moving) lastDriverActivity = millis();
  updateRamp();
  sendCompleted();
  if (fanReportPending && Serial.availableForWrite() >= 55) fanReport();
  if (!moving && queueCount && completedCount < COMPLETION_CAPACITY) startNextQueued();
  if (enabled && !moving && !queueCount &&
      millis() - lastDriverActivity > IDLE_RELEASE_MS) disableDrivers();
}
