/* NB3 demo: continuous-rotation servos, right D9, left D10.
   USB serial 115200. f/b/l/r/x; ? identifies this firmware.
   Every movement requires another command within 600 ms.
   Neutral and slow speed must be checked with the wheels raised first. */
#include <Servo.h>

Servo leftMotor, rightMotor;
const int LEFT_NEUTRAL = 90;
const int RIGHT_NEUTRAL = 90;
const int SPEED = 12;
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
  rightMotor.attach(9);
  leftMotor.attach(10);
  stopMotors();
  Serial.begin(115200);
}

void loop() {
  while (Serial.available()) {
    char command = Serial.read();
    int left = 0, right = 0;
    switch (command) {
      case '?': Serial.println("NB3-DEMO-1 SERVO WATCHDOG=600 SPEED=12"); continue;
      case 'x': stopMotors(); continue;
      case 'f': left = SPEED; right = -SPEED; break;
      case 'b': left = -SPEED; right = SPEED; break;
      case 'l': left = -SPEED; right = -SPEED; break;
      case 'r': left = SPEED; right = SPEED; break;
      default: stopMotors(); continue;
    }
    leftMotor.write(LEFT_NEUTRAL + left);
    rightMotor.write(RIGHT_NEUTRAL + right);
    lastCommand = millis();
    moving = true;
    digitalWrite(LED_BUILTIN, HIGH);
  }
  if (moving && millis() - lastCommand > TIMEOUT_MS) stopMotors();
}
