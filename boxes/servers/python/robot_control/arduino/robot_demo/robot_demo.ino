/* NB3 demo: continuous-rotation servos, physical left D9, right D10.
   USB serial 115200. f/b/l/r/x; q/e/z/c for curves; ? identifies firmware.
   Every movement requires another command within 600 ms.
   Neutral and slow speed must be checked with the wheels raised first. */
#include <Servo.h>

Servo leftMotor, rightMotor;
const int LEFT_PIN = 9;
const int RIGHT_PIN = 10;
// Confirmed on this build: lower pulses drive the left wheel forward;
// higher pulses drive the right wheel forward (toward the camera).
const int LEFT_FORWARD_SIGN = -1;
const int RIGHT_FORWARD_SIGN = 1;
const int LEFT_NEUTRAL = 90;
const int RIGHT_NEUTRAL = 90;
const int SPEED = 12;
const int INNER_SPEED = 6;
const unsigned long TIMEOUT_MS = 600;
unsigned long lastCommand = 0;
bool moving = false;

void stopMotors() {
  leftMotor.write(LEFT_NEUTRAL);
  rightMotor.write(RIGHT_NEUTRAL);
  digitalWrite(LED_BUILTIN, LOW);
  moving = false;
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
  while (Serial.available()) {
    char command = Serial.read();
    int left = 0, right = 0;
    switch (command) {
      case '?': Serial.println("NB3-DEMO-3 SERVO WATCHDOG=600 SPEED=12 LEFT=9 RIGHT=10"); continue;
      case 'x': stopMotors(); continue;
      // Logical wheel speeds: positive is forward on either physical wheel.
      case 'f': left = SPEED; right = SPEED; break;
      case 'b': left = -SPEED; right = -SPEED; break;
      case 'l': left = -SPEED; right = SPEED; break;
      case 'r': left = SPEED; right = -SPEED; break;
      // Both wheels keep moving, with the inner wheel turning more slowly.
      case 'q': left = INNER_SPEED; right = SPEED; break; // Forward-left
      case 'e': left = SPEED; right = INNER_SPEED; break; // Forward-right
      // In reverse, the rear of the robot curves toward the selected side.
      case 'z': left = -INNER_SPEED; right = -SPEED; break; // Backward-left
      case 'c': left = -SPEED; right = -INNER_SPEED; break; // Backward-right
      default: stopMotors(); continue;
    }
    leftMotor.write(LEFT_NEUTRAL + LEFT_FORWARD_SIGN * left);
    rightMotor.write(RIGHT_NEUTRAL + RIGHT_FORWARD_SIGN * right);
    lastCommand = millis();
    moving = true;
    digitalWrite(LED_BUILTIN, HIGH);
  }
  if (moving && millis() - lastCommand > TIMEOUT_MS) stopMotors();
}
