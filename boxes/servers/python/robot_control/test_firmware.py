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
struct Servo {int value=0; void attach(int){} void write(int v){value=v;}};
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
  command('f'); wheels(102,78);
  command('e'); wheels(102,84); // Forward-right: left wheel faster
  command('f'); wheels(102,78); // Release right, keep forward
  command('q'); wheels(96,78);  // Forward-left: right wheel faster
  command('b'); wheels(78,102);
  command('z'); wheels(84,102); // Reverse, rear curves left
  command('c'); wheels(78,96);  // Reverse, rear curves right
  command('l'); wheels(78,78);
  command('r'); wheels(102,102);
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
  assert(Serial.reply=="NB3-DEMO-2 SERVO WATCHDOG=600 SPEED=12 STEERING=1");
}
''')
            subprocess.run(["g++", "-I", directory, str(root / "check.cpp"),
                            "-o", str(root / "check")], check=True, capture_output=True)
            subprocess.run([str(root / "check")], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
