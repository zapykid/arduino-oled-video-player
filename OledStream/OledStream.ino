// OLED video stream receiver for Arduino Nano + 128x64 I2C OLED (SSD1306 or SH1106).
// The Nano asks for each frame with 'R'; the PC app (oled_player.py) answers with
//   0xAA 0x55, 1024 bytes of pixels, 1 byte XOR checksum
// The marker lets the Nano find the start of a frame again if bytes ever get lost or late,
// and the checksum stops a damaged frame from reaching the screen.
//
// Wiring:  OLED GND -> GND   OLED VCC -> 5V   OLED SCL -> A5   OLED SDA -> A4
//
// If the onboard LED keeps blinking, the display was not found - check the wiring.

#include <Wire.h>

#define COL_OFFSET 0      // 0 for SSD1306 (most 0.96" boards), 2 for SH1106 (most 1.3" boards)
#define I2C_SPEED 400000  // 800000 is faster but glitches on some displays
#define BAUD 1000000      // must match BAUD in oled_player.py

uint8_t fb[1024];         // one frame: 8 pages x 128 columns, 1 bit per pixel
uint8_t addr = 0;         // I2C address of the display, 0 = not found

bool findDisplay() {
  for (uint8_t a = 0x3C; a <= 0x3D; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { addr = a; return true; }
  }
  addr = 0;
  return false;
}

void initDisplay() {
  static const uint8_t init[] PROGMEM = {
    0xAE,             // display off
    0xD5, 0x80,       // clock divide
    0xA8, 0x3F,       // multiplex 64
    0xD3, 0x00,       // display offset 0
    0x40,             // start line 0
    0x8D, 0x14,       // charge pump on
    0x20, 0x02,       // page addressing mode
    0xA1, 0xC8,       // flip so (0,0) is top-left
    0xDA, 0x12,       // COM pins
    0x81, 0x7F,       // contrast (lower = less load on cheap panels)
    0xD9, 0x22,       // precharge (gentler, avoids dark rows on some clones)
    0xDB, 0x20,       // VCOM detect
    0xA4, 0xA6,       // show RAM, normal (not inverted)
    0xAF              // display on
  };
  Wire.beginTransmission(addr);
  Wire.write(0x00);
  for (uint8_t i = 0; i < sizeof(init); i++) Wire.write(pgm_read_byte(&init[i]));
  Wire.endTransmission();
}

// Returns false if the display stopped answering.
bool pushFrame() {
  bool ok = true;
  for (uint8_t p = 0; p < 8 && ok; p++) {
    Wire.beginTransmission(addr);
    Wire.write(0x00);
    Wire.write(0xB0 | p);
    Wire.write(COL_OFFSET & 0x0F);
    Wire.write(0x10 | (COL_OFFSET >> 4));
    ok = Wire.endTransmission() == 0;

    const uint8_t *row = fb + p * 128;
    for (uint8_t x = 0; x < 128 && ok; ) {
      uint8_t n = 128 - x;
      if (n > 31) n = 31;   // Wire's buffer holds 32 bytes including the 0x40 prefix
      Wire.beginTransmission(addr);
      Wire.write(0x40);
      Wire.write(row + x, n);
      ok = Wire.endTransmission() == 0;
      x += n;
    }
  }
  return ok;
}

// If the Nano is reset (e.g. when the PC app opens the port) while it is sending to the
// display, the display can be left holding SDA low and won't answer. Clocking SCL lets it
// finish its byte and let go, then a STOP condition resets the bus.
void i2cRecover() {
  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, OUTPUT);
  for (uint8_t i = 0; i < 18 && digitalRead(SDA) == LOW; i++) {
    digitalWrite(SCL, LOW);
    delayMicroseconds(5);
    digitalWrite(SCL, HIGH);
    delayMicroseconds(5);
  }
  pinMode(SDA, OUTPUT);
  digitalWrite(SDA, LOW);
  delayMicroseconds(5);
  digitalWrite(SDA, HIGH);   // SDA rising while SCL is high = STOP
  delayMicroseconds(5);
  pinMode(SDA, INPUT);
  pinMode(SCL, INPUT);
}

void connectDisplay() {
  Wire.end();
  i2cRecover();
  Wire.begin();
  Wire.setClock(I2C_SPEED);
  Wire.setWireTimeout(3000, true);   // never hang forever on a stuck bus
  if (findDisplay()) {
    initDisplay();
    memset(fb, 0, sizeof(fb));
    if (!pushFrame()) addr = 0;      // clear leftover garbage
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(BAUD);
  Serial.setTimeout(150);
  connectDisplay();
}

// Skip bytes until the 0xAA 0x55 frame marker. Gives up after 300 ms.
bool waitMarker() {
  uint8_t prev = 0;
  unsigned long start = millis();
  while (millis() - start < 300) {
    if (!Serial.available()) continue;
    uint8_t b = Serial.read();
    if (prev == 0xAA && b == 0x55) return true;
    prev = b;
  }
  return false;
}

bool readFrame() {
  if (!waitMarker()) return false;
  if (Serial.readBytes(fb, sizeof(fb)) != sizeof(fb)) return false;
  uint8_t check;
  if (Serial.readBytes(&check, 1) != 1) return false;
  for (uint16_t i = 0; i < sizeof(fb); i++) check ^= fb[i];
  if (check != 0) Serial.write('!');   // tell the PC a frame was damaged and skipped
  return check == 0;
}

void loop() {
  static unsigned long lastTry = 0;

  Serial.write('R');
  if (readFrame()) {
    if (addr && !pushFrame()) addr = 0;    // display stopped answering
  } else {
    while (Serial.available()) Serial.read();   // drop anything half-received
  }

  // No display: blink the LED and keep trying to reconnect, even while frames stream in.
  if (!addr) {
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
    if (millis() - lastTry > 500) {
      lastTry = millis();
      connectDisplay();
    }
  } else {
    digitalWrite(LED_BUILTIN, LOW);
  }
}
