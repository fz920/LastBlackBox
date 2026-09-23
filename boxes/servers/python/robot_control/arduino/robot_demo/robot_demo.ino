/* NB3 demo: continuous-rotation servos, physical left D9, right D10.
   USB serial 115200. f/b/l/r/x; q/e/z/c for curves; ? identifies firmware.
   Uppercase movement commands select full speed, limited to 2 seconds.
   Calibrated slow motion: @ followed by two uppercase hex servo values and LF.
   Every movement requires another command within 600 ms.
   Neutral and slow speed must be checked with the wheels raised first. */
#include <Servo.h>

Servo leftMotor, rightMotor;
const int LEFT_PIN = 9;
const int RIGHT_PIN = 10;
// Retain the original lowercase protocol signs. The website corrects directions
// and sends physical servo values for calibrated slow movement.
const int LEFT_FORWARD_SIGN = -1;
const int RIGHT_FORWARD_SIGN = 1;
const int LEFT_NEUTRAL = 90;
const int RIGHT_NEUTRAL = 90;
const int SPEED = 12;
const int INNER_SPEED = 6;
const int FULL_SPEED = 90;
const int FULL_INNER_SPEED = 45;
const unsigned long TIMEOUT_MS = 600;
const unsigned long FULL_LIMIT_MS = 2000;
unsigned long lastCommand = 0;
unsigned long fullStarted = 0;
bool moving = false;
bool fullMoving = false;
bool fullBlocked = false;
char packet[4];
int packetLength = -1;
bool discarding = false;
unsigned long packetStarted = 0;

int hexDigit(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  return -1;
}

void stopMotors() {
  leftMotor.write(LEFT_NEUTRAL);
  rightMotor.write(RIGHT_NEUTRAL);
  digitalWrite(LED_BUILTIN, LOW);
  moving = false;
  fullMoving = false;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  leftMotor.write(LEFT_NEUTRAL);
  rightMotor.write(RIGHT_NEUTRAL);
  leftMotor.attach(LEFT_PIN);
  rightMotor.attach(RIGHT_PIN);
  stopMotors();
  Serial.begin(115200);
}

void loop() {
  if (packetLength >= 0 && millis() - packetStarted > 50) {
    stopMotors();
    packetLength = -1;
    discarding = true;
  }
  // Latch the limit until Stop so queued heartbeats cannot restart a test.
  if (fullMoving && millis() - fullStarted >= FULL_LIMIT_MS) {
    stopMotors();
    fullBlocked = true;
  }
  while (Serial.available()) {
    char command = Serial.read();
    // Stop always wins, even during an incomplete or malformed packet.
    if (command == 'x') {
      stopMotors(); fullBlocked = false; packetLength = -1; discarding = false;
      continue;
    }
    if (command == '@') {
      packetLength = 0; packetStarted = millis(); discarding = false;
      continue;
    }
    if (packetLength >= 0) {
      if (packetLength < 4 && hexDigit(command) >= 0) {
        packet[packetLength++] = command;
        continue;
      }
      if (packetLength == 4 && command == '\n') {
        int left = 16 * hexDigit(packet[0]) + hexDigit(packet[1]);
        int right = 16 * hexDigit(packet[2]) + hexDigit(packet[3]);
        if (left >= 66 && left <= 114 && right >= 66 && right <= 114) {
          leftMotor.write(left); rightMotor.write(right);
          fullMoving = false;
          moving = left != LEFT_NEUTRAL || right != RIGHT_NEUTRAL;
          lastCommand = millis();
          digitalWrite(LED_BUILTIN, moving ? HIGH : LOW);
        } else stopMotors();
      } else stopMotors();
      packetLength = -1;
      discarding = command != '\n';
      continue;
    }
    if (discarding) {
      if (command == '\n') discarding = false;
      continue;
    }
    bool full = command == 'F' || command == 'B' || command == 'L' || command == 'R'
             || command == 'Q' || command == 'E' || command == 'Z' || command == 'C';
    if (full && fullBlocked) continue;
    if (full) command += 'a' - 'A';
    int speed = full ? FULL_SPEED : SPEED;
    int inner = full ? FULL_INNER_SPEED : INNER_SPEED;
    int left = 0, right = 0;
    switch (command) {
      case '?': Serial.println("NB3-DEMO-5 SERVO WATCHDOG=600 SPEED=12 FULL=90 LIMIT=2000 TRIM=24 LEFT=9 RIGHT=10"); continue;
      // Logical wheel speeds: positive is forward on either physical wheel.
      case 'f': left = speed; right = speed; break;
      case 'b': left = -speed; right = -speed; break;
      case 'l': left = -speed; right = speed; break;
      case 'r': left = speed; right = -speed; break;
      // Both wheels keep moving, with the inner wheel turning more slowly.
      case 'q': left = inner; right = speed; break; // Forward-left
      case 'e': left = speed; right = inner; break; // Forward-right
      // In reverse, the rear of the robot curves toward the selected side.
      case 'z': left = -inner; right = -speed; break; // Backward-left
      case 'c': left = -speed; right = -inner; break; // Backward-right
      default: stopMotors(); continue;
    }
    if (full && !fullMoving) fullStarted = millis();
    fullMoving = full;
    leftMotor.write(LEFT_NEUTRAL + LEFT_FORWARD_SIGN * left);
    rightMotor.write(RIGHT_NEUTRAL + RIGHT_FORWARD_SIGN * right);
    lastCommand = millis();
    moving = true;
    digitalWrite(LED_BUILTIN, HIGH);
  }
  if (moving && millis() - lastCommand > TIMEOUT_MS) stopMotors();
}
