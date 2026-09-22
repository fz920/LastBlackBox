"""Compile the actual sketch against fake hardware; never touches the Arduino."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("g++"), "g++ needed for firmware simulation")
class FirmwareTests(unittest.TestCase):
    def test_wheel_outputs_and_watchdog(self):
        sketch = Path(__file__).resolve().parent / "arduino/robot_demo/robot_demo.ino"
        with tempfile.TemporaryDirectory(prefix="nb3-steering-test-") as directory:
            root = Path(directory)
            (root / "Servo.h").write_text('''
#pragma once
#include <deque>
#include <string>
const int LED_BUILTIN=13, OUTPUT=1, LOW=0, HIGH=1;
unsigned long nowMs=0;
unsigned long millis(){return nowMs;}
void pinMode(int,int){}
void digitalWrite(int,int){}
struct Servo {int value=0,pin=-1; void attach(int p){pin=p;} void write(int v){value=v;}};
struct SerialMock {
  std::deque<char> input; std::string reply;
  void begin(int){} bool available(){return !input.empty();}
  char read(){char c=input.front();input.pop_front();return c;}
  void println(const char* s){reply=s;}
} Serial;
''')
            (root / "check.cpp").write_text('#include <cassert>\n#include "' + str(sketch) + '"\n' + '''
void command(char c){Serial.input.push_back(c);loop();}
void wheels(int l,int r){assert(leftMotor.value==l);assert(rightMotor.value==r);}
int main(){
  setup(); wheels(90,90); assert(!moving);
  assert(leftMotor.pin==9 && rightMotor.pin==10);
  command('f'); wheels(78,102); // Preserve the confirmed physical forward signals
  command('e'); wheels(78,96);  // Forward-right: reduce the physical right wheel
  command('f'); wheels(78,102); // Release right, keep forward
  command('q'); wheels(84,102); // Forward-left: reduce the physical left wheel
  command('b'); wheels(102,78);
  command('z'); wheels(96,78);  // Reverse, rear curves left
  command('c'); wheels(102,84); // Reverse, rear curves right
  command('l'); wheels(102,102);
  command('r'); wheels(78,78);
  command('x'); wheels(90,90); assert(!moving);
  // Every curved movement must stop if its commands stop arriving.
  for(char c : {'q','e','z','c'}){
    nowMs=100; command(c); assert(moving);
    nowMs=600; command(c); // Heartbeat extends the deadline
    nowMs=1199; loop(); assert(moving);
    nowMs=1201; loop(); wheels(90,90); assert(!moving);
  }
  command('e'); command('!'); wheels(90,90); assert(!moving);
  command('?');
  assert(Serial.reply=="NB3-DEMO-3 SERVO WATCHDOG=600 SPEED=12 LEFT=9 RIGHT=10");
}
''')
            subprocess.run(["g++", "-I", directory, str(root / "check.cpp"),
                            "-o", str(root / "check")], check=True, capture_output=True)
            subprocess.run([str(root / "check")], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
