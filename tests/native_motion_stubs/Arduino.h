#pragma once
#include <stdint.h>
#include <string>
#include <sstream>

typedef uint8_t byte;
struct __FlashStringHelper;
#define F(value) reinterpret_cast<const __FlashStringHelper *>(value)
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define A0 14
#define A1 15
#define A2 16
#define PD2 2
#define PD3 3
#define PC0 0
#define PC1 1
#define OCIE2A 1
#define OCF2A 1
#define CS21 1
#define _BV(bit) (1U << (bit))

extern uint8_t PORTD, PORTC, TCCR2A, TCCR2B, TIMSK2, TIFR2, OCR2A, TCNT2;
extern uint64_t fakeTicks;
extern int fakePins[20];
inline unsigned long millis() { return (uint32_t)(fakeTicks / 2000); }
inline void delayMicroseconds(unsigned int us) { fakeTicks += us * 2; TCNT2 = fakeTicks; }
inline void digitalWrite(byte pin, byte value) { fakePins[pin] = value; }
inline void pinMode(byte, byte) {}

struct NativeSerial {
  std::string output;
  void begin(unsigned long) {}
  int available() { return 0; }
  int availableForWrite() { return 64; }
  int read() { return -1; }
  void print(const __FlashStringHelper *value) { output += reinterpret_cast<const char *>(value); }
  template<class T> void print(T value) { std::ostringstream stream; stream << value; output += stream.str(); }
  void print(byte value) { print((unsigned int)value); }
  void println() { output += '\n'; }
  template<class T> void println(T value) { print(value); println(); }
};
extern NativeSerial Serial;
