/*
  Lin Engineering 8718S-39D stepper controller
  Arduino Uno + STEP/DIR driver, controlled over USB serial.

  Wiring retained from the LCD version:
    STEP = D2, DIR = D3, EN = D10 (active LOW)

  Serial: 115200 baud, ASCII lines ending in \n.
  Commands:
    STATUS
    CONFIG <rpm> <microsteps> <jog_deg> <move_deg> <osc_deg> <dwell_ms> <display_divisor>
                                     e.g. CONFIG 80 32 5 90 90 200 4
    RUN CW|CCW
    MOVE <degrees> CW|CCW           e.g. MOVE 90 CW
    OSC <degrees> <dwell_ms>         e.g. OSC 90 200
    STOP
    ENABLE ON|OFF
    ZERO

  Every command except STATUS returns OK or ERR, followed by STATUS
  on success. STATUS returns one STATUS line. The microstep value is
  software calibration only: it MUST match the driver's physical setting.
*/

#include <Arduino.h>
#include <ctype.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

const uint8_t STEP_PIN = 2;
const uint8_t DIR_PIN = 3;
const uint8_t EN_PIN = 10;
const uint16_t MOTOR_STEPS = 200;  // 1.8 degrees per full step
const float MIN_RPM = 5.0f;
const float MAX_RPM = 200.0f;
const float MAX_STEP_HZ = 25000.0f;
const float MIN_DEGREES = 0.1f;
const float MAX_DEGREES = 3600.0f;
const unsigned long HOST_TIMEOUT_MS = 2500;

float rpm = 80.0f;
uint16_t microsteps = 32;
volatile bool directionCW = true;
bool driverEnabled = true;
float jogDegrees = 5.0f;
float moveDegrees = 90.0f;
float displayDivisor = 4.0f;  // Calibrates the displayed tip, not motor pulses.

volatile bool motorRunning = false;
volatile bool finiteMove = false;
volatile uint32_t stepsRemaining = 0;
volatile bool stepState = false;
volatile bool stopAfterLow = false;
volatile int32_t pulseDelta = 0;
int64_t signedStepCount = 0;     // Signed STEP pulses since ZERO.
int64_t position256 = 0;        // Position in 1/256-full-step units.

enum MotorMode { IDLE, CONTINUOUS, FINITE_MOVE, OSCILLATING };
MotorMode mode = IDLE;
float oscillationDegrees = 90.0f;
uint16_t oscillationDwellMs = 200;
bool oscillationDirectionCW = true;
bool oscillationWaiting = false;
unsigned long oscillationReadyAt = 0;

char inputLine[72];
uint8_t inputLength = 0;
bool inputOverflow = false;
unsigned long lastHostContact = 0;

bool validConfiguration(float requestedRpm, uint16_t requestedMicrosteps) {
  if (isnan(requestedRpm) || isinf(requestedRpm) ||
      requestedRpm < MIN_RPM || requestedRpm > MAX_RPM) return false;
  if (requestedMicrosteps < 1 || requestedMicrosteps > 256 ||
      (requestedMicrosteps & (requestedMicrosteps - 1)) != 0) return false;
  const float stepHz = requestedRpm * MOTOR_STEPS * requestedMicrosteps / 60.0f;
  // The low-speed limit also keeps Timer1's 16-bit compare value in range.
  return stepHz >= 16.0f && stepHz <= MAX_STEP_HZ;
}

uint16_t timerCompareValue() {
  const float stepHz = rpm * MOTOR_STEPS * microsteps / 60.0f;
  return (uint16_t)(F_CPU / (16.0f * stepHz) - 1.0f + 0.5f);
}

void stopPulses() {
  noInterrupts();
  TCCR1B &= ~((1 << CS12) | (1 << CS11) | (1 << CS10));
  motorRunning = false;
  finiteMove = false;
  stepsRemaining = 0;
  stepState = false;
  stopAfterLow = false;
  PORTD &= ~(1 << PD2);
  interrupts();
}

void stopAll() {
  stopPulses();
  mode = IDLE;
  oscillationWaiting = false;
}

void flushStepCount() {
  noInterrupts();
  int32_t delta = pulseDelta;
  pulseDelta = 0;
  interrupts();
  if (delta != 0) {
    signedStepCount += delta;
    position256 += (int64_t)delta * (256 / microsteps);
  }
}

void printInt64(int64_t value) {
  char digits[21];
  uint8_t length = 0;
  if (value < 0) Serial.print('-');
  uint64_t magnitude = value < 0 ? (uint64_t)(-(value + 1)) + 1 : (uint64_t)value;
  do {
    digits[length++] = (char)('0' + magnitude % 10);
    magnitude /= 10;
  } while (magnitude != 0);
  while (length > 0) Serial.print(digits[--length]);
}

void setDirection(bool clockwise) {
  directionCW = clockwise;
  digitalWrite(DIR_PIN, clockwise ? HIGH : LOW);
  delayMicroseconds(10);
}

void startPulses(uint32_t count, bool finite, bool clockwise) {
  stopPulses();
  setDirection(clockwise);
  noInterrupts();
  OCR1A = timerCompareValue();
  TCNT1 = 0;
  TIFR1 = (1 << OCF1A);
  stepsRemaining = count;
  finiteMove = finite;
  stepState = false;
  stopAfterLow = false;
  PORTD &= ~(1 << PD2);
  TCCR1A = 0;
  TCCR1B = (1 << WGM12) | (1 << CS11);  // CTC, prescaler 8
  motorRunning = true;
  interrupts();
}

uint32_t angleToSteps(float degrees) {
  uint32_t count = (uint32_t)(degrees * MOTOR_STEPS * microsteps / 360.0f + 0.5f);
  return count == 0 ? 1 : count;
}

void startOscillationStroke() {
  startPulses(angleToSteps(oscillationDegrees), true, oscillationDirectionCW);
  oscillationDirectionCW = !oscillationDirectionCW;
  oscillationWaiting = false;
}

void updateMotion() {
  if (mode == FINITE_MOVE && !motorRunning) mode = IDLE;
  if (mode != OSCILLATING || motorRunning) return;

  if (!oscillationWaiting) {
    oscillationReadyAt = millis() + oscillationDwellMs;
    oscillationWaiting = true;
  }
  if ((long)(millis() - oscillationReadyAt) >= 0) startOscillationStroke();
}

ISR(TIMER1_COMPA_vect) {
  stepState = !stepState;
  if (stepState) {
    PORTD |= (1 << PD2);
    pulseDelta += directionCW ? 1 : -1;
    if (finiteMove && stepsRemaining > 0) {
      --stepsRemaining;
      if (stepsRemaining == 0) stopAfterLow = true;
    }
  } else {
    PORTD &= ~(1 << PD2);
    if (stopAfterLow) {
      TCCR1B &= ~((1 << CS12) | (1 << CS11) | (1 << CS10));
      motorRunning = false;
      finiteMove = false;
      stopAfterLow = false;
      stepState = false;
    }
  }
}

void printStatus() {
  flushStepCount();
  noInterrupts();
  uint32_t remainingSnapshot = stepsRemaining;
  bool runningSnapshot = motorRunning;
  interrupts();
  Serial.print(F("STATUS mode="));
  switch (mode) {
    case IDLE: Serial.print(F("IDLE")); break;
    case CONTINUOUS: Serial.print(F("CONT")); break;
    case FINITE_MOVE: Serial.print(F("MOVE")); break;
    case OSCILLATING: Serial.print(F("OSC")); break;
  }
  Serial.print(F(" running=")); Serial.print(runningSnapshot ? 1 : 0);
  Serial.print(F(" rpm=")); Serial.print(rpm, 2);
  Serial.print(F(" microsteps=")); Serial.print(microsteps);
  Serial.print(F(" direction=")); Serial.print(directionCW ? F("CW") : F("CCW"));
  Serial.print(F(" enabled=")); Serial.print(driverEnabled ? 1 : 0);
  Serial.print(F(" jog=")); Serial.print(jogDegrees, 1);
  Serial.print(F(" move=")); Serial.print(moveDegrees, 1);
  Serial.print(F(" osc=")); Serial.print(oscillationDegrees, 1);
  Serial.print(F(" dwell=")); Serial.print(oscillationDwellMs);
  Serial.print(F(" display_divisor=")); Serial.print(displayDivisor, 2);
  Serial.print(F(" remaining=")); Serial.print(remainingSnapshot);
  Serial.print(F(" steps=")); printInt64(signedStepCount);
  Serial.print(F(" units=")); printInt64(position256);
  Serial.println();
}

void replyOk() {
  Serial.println(F("OK"));
  printStatus();
}

void replyError(const __FlashStringHelper *message) {
  Serial.print(F("ERR "));
  Serial.println(message);
}

bool parseFloat(const char *token, float &value) {
  if (!token) return false;
  char *end;
  value = (float)strtod(token, &end);
  return end != token && *end == '\0' && !isnan(value) && !isinf(value);
}

bool parseUnsigned(const char *token, unsigned long &value) {
  if (!token || !*token || *token == '-') return false;
  char *end;
  value = strtoul(token, &end, 10);
  return end != token && *end == '\0';
}

bool parseDirection(const char *token, bool &clockwise) {
  if (!token) return false;
  if (strcmp(token, "CW") == 0) { clockwise = true; return true; }
  if (strcmp(token, "CCW") == 0) { clockwise = false; return true; }
  return false;
}

bool validDegrees(float degrees) {
  return degrees >= MIN_DEGREES && degrees <= MAX_DEGREES;
}

void handleCommand(char *line) {
  for (char *p = line; *p; ++p) *p = (char)toupper((unsigned char)*p);
  char *save = NULL;
  char *cmd = strtok_r(line, " \t", &save);
  if (!cmd) return;

  if (strcmp(cmd, "STATUS") == 0) {
    if (strtok_r(NULL, " \t", &save)) replyError(F("syntax"));
    else printStatus();
    return;
  }

  if (strcmp(cmd, "STOP") == 0) {
    if (strtok_r(NULL, " \t", &save)) { replyError(F("syntax")); return; }
    stopAll();
    replyOk();
    return;
  }

  if (strcmp(cmd, "ZERO") == 0) {
    if (strtok_r(NULL, " \t", &save)) { replyError(F("syntax")); return; }
    if (mode != IDLE || motorRunning) { replyError(F("stop_before_zero")); return; }
    flushStepCount();
    signedStepCount = 0;
    position256 = 0;
    replyOk();
    return;
  }

  if (strcmp(cmd, "CONFIG") == 0) {
    float requestedRpm;
    float requestedJog = jogDegrees;
    float requestedMove = moveDegrees;
    float requestedOsc = oscillationDegrees;
    float requestedDisplayDivisor = displayDivisor;
    unsigned long requestedMicrosteps;
    unsigned long requestedDwell = oscillationDwellMs;
    char *rpmToken = strtok_r(NULL, " \t", &save);
    char *microToken = strtok_r(NULL, " \t", &save);
    char *jogToken = strtok_r(NULL, " \t", &save);
    char *moveToken = jogToken ? strtok_r(NULL, " \t", &save) : NULL;
    char *oscToken = jogToken ? strtok_r(NULL, " \t", &save) : NULL;
    char *dwellToken = jogToken ? strtok_r(NULL, " \t", &save) : NULL;
    char *divisorToken = jogToken ? strtok_r(NULL, " \t", &save) : NULL;
    if (!parseFloat(rpmToken, requestedRpm) ||
        !parseUnsigned(microToken, requestedMicrosteps) ||
        (jogToken && (!parseFloat(jogToken, requestedJog) ||
                      !parseFloat(moveToken, requestedMove) ||
                      !parseFloat(oscToken, requestedOsc) ||
                      !parseUnsigned(dwellToken, requestedDwell))) ||
        (divisorToken && !parseFloat(divisorToken, requestedDisplayDivisor)) ||
        strtok_r(NULL, " \t", &save) ||
        requestedMicrosteps > 256 ||
        !validDegrees(requestedJog) || !validDegrees(requestedMove) ||
        !validDegrees(requestedOsc) || requestedDwell > 5000 ||
        requestedDisplayDivisor < 1.0f || requestedDisplayDivisor > 16.0f ||
        !validConfiguration(requestedRpm, (uint16_t)requestedMicrosteps)) {
      replyError(F("invalid_config")); return;
    }
    if (requestedRpm != rpm || requestedMicrosteps != microsteps) {
      stopAll();
      flushStepCount();  // Attribute all previous pulses using the old microstep size.
    }
    rpm = requestedRpm;
    microsteps = (uint16_t)requestedMicrosteps;
    jogDegrees = requestedJog;
    moveDegrees = requestedMove;
    oscillationDegrees = requestedOsc;
    oscillationDwellMs = (uint16_t)requestedDwell;
    displayDivisor = requestedDisplayDivisor;
    replyOk();
    return;
  }

  if (strcmp(cmd, "ENABLE") == 0) {
    char *arg = strtok_r(NULL, " \t", &save);
    if (!arg || strtok_r(NULL, " \t", &save) ||
        (strcmp(arg, "ON") != 0 && strcmp(arg, "OFF") != 0)) {
      replyError(F("syntax")); return;
    }
    if (strcmp(arg, "OFF") == 0) stopAll();
    driverEnabled = strcmp(arg, "ON") == 0;
    digitalWrite(EN_PIN, driverEnabled ? LOW : HIGH);
    replyOk();
    return;
  }

  if (strcmp(cmd, "RUN") == 0) {
    bool clockwise;
    char *arg = strtok_r(NULL, " \t", &save);
    if (!parseDirection(arg, clockwise) || strtok_r(NULL, " \t", &save)) {
      replyError(F("syntax")); return;
    }
    if (!driverEnabled) { replyError(F("driver_disabled")); return; }
    stopAll();
    startPulses(0, false, clockwise);
    mode = CONTINUOUS;
    replyOk();
    return;
  }

  if (strcmp(cmd, "MOVE") == 0) {
    float degrees;
    bool clockwise;
    char *angleToken = strtok_r(NULL, " \t", &save);
    char *directionToken = strtok_r(NULL, " \t", &save);
    if (!parseFloat(angleToken, degrees) || !validDegrees(degrees) ||
        !parseDirection(directionToken, clockwise) || strtok_r(NULL, " \t", &save)) {
      replyError(F("invalid_move")); return;
    }
    if (!driverEnabled) { replyError(F("driver_disabled")); return; }
    stopAll();
    startPulses(angleToSteps(degrees), true, clockwise);
    mode = FINITE_MOVE;
    replyOk();
    return;
  }

  if (strcmp(cmd, "OSC") == 0) {
    float degrees;
    unsigned long dwell;
    char *angleToken = strtok_r(NULL, " \t", &save);
    char *dwellToken = strtok_r(NULL, " \t", &save);
    if (!parseFloat(angleToken, degrees) || !validDegrees(degrees) ||
        !parseUnsigned(dwellToken, dwell) || dwell > 5000 ||
        strtok_r(NULL, " \t", &save)) {
      replyError(F("invalid_osc")); return;
    }
    if (!driverEnabled) { replyError(F("driver_disabled")); return; }
    stopAll();
    oscillationDegrees = degrees;
    oscillationDwellMs = (uint16_t)dwell;
    oscillationDirectionCW = true;
    mode = OSCILLATING;
    startOscillationStroke();
    replyOk();
    return;
  }

  replyError(F("unknown_command"));
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lastHostContact = millis();
      if (inputOverflow) replyError(F("line_too_long"));
      else { inputLine[inputLength] = '\0'; handleCommand(inputLine); }
      inputLength = 0;
      inputOverflow = false;
    } else if (!inputOverflow) {
      if (inputLength < sizeof(inputLine) - 1) inputLine[inputLength++] = c;
      else inputOverflow = true;
    }
  }
}

void setup() {
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(EN_PIN, OUTPUT);
  digitalWrite(STEP_PIN, LOW);
  digitalWrite(DIR_PIN, HIGH);
  digitalWrite(EN_PIN, LOW);  // Change to HIGH if your driver enables on HIGH.

  noInterrupts();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  TIMSK1 |= (1 << OCIE1A);
  interrupts();

  Serial.begin(115200);
  Serial.println(F("READY serial_stepper_controller"));
  printStatus();
}

void loop() {
  readSerial();
  if (mode != IDLE && millis() - lastHostContact > HOST_TIMEOUT_MS) {
    stopAll();
    Serial.println(F("ERR host_timeout"));
    printStatus();
  }
  updateMotion();
}
