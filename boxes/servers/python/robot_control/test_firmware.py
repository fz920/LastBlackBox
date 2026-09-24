"""Compile the actual sketch against fake hardware; never touches the Arduino."""
from pathlib import Path
import shutil
import json
import subprocess
import tempfile
import unittest

from server import Controller, Frames


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
            # Exercise logical website commands through the real controller and
            # sketch, checking the corrected electrical output on each wheel.
            class SerialCapture:
                def __init__(self):
                    self.byte = None

                def write(self, data):
                    self.byte = data
                    return len(data)

            frames = Frames()
            port = SerialCapture()
            controller = Controller(frames, port)
            token = controller.claim()
            checks = []
            expected = [
                ("forward", 102, 78), ("backward", 78, 102),
                ("left", 78, 78), ("right", 102, 102),
                ("forward_left", 96, 78), ("forward_right", 102, 84),
                ("backward_left", 84, 102), ("backward_right", 78, 96),
                ("stop", 90, 90),
            ]
            for sequence, (direction, left, right) in enumerate(expected):
                frames.write(b"jpeg")
                controller.control(token, sequence, direction, frames.timestamp)
                self.assertEqual(controller.status()["command"], direction)
                checks.append(f"send({json.dumps(port.byte.decode())}); wheels({left},{right});")
            controller.stop_all()
            controller.set_calibration({"left_forward": 10, "right_forward": 11,
                                        "left_backward": 12, "right_backward": 13})
            token = controller.claim()
            expected_custom = [
                ("forward", 100, 79), ("backward", 78, 103),
                ("left", 78, 79), ("right", 100, 103),
                ("forward_left", 95, 79), ("forward_right", 100, 84),
                ("backward_left", 84, 103), ("backward_right", 78, 97),
                ("stop", 90, 90),
            ]
            for sequence, (direction, left, right) in enumerate(expected_custom):
                frames.write(b"jpeg")
                controller.control(token, sequence, direction, frames.timestamp)
                checks.append(f"send({json.dumps(port.byte.decode())}); wheels({left},{right});")
            controller.stop_all()
            token = controller.claim("full")
            expected_full = [
                ("forward", 180, 0), ("backward", 0, 180),
                ("left", 0, 0), ("right", 180, 180),
                ("forward_left", 135, 0), ("forward_right", 180, 45),
                ("backward_left", 45, 180), ("backward_right", 0, 135),
                ("stop", 90, 90),
            ]
            for sequence, (direction, left, right) in enumerate(expected_full):
                frames.write(b"jpeg")
                controller.control(token, sequence, direction, frames.timestamp)
                checks.append(f"send({json.dumps(port.byte.decode())}); wheels({left},{right});")

            (root / "check.cpp").write_text('#include <cassert>\n#include "' + str(sketch) + '"\n' + '''
void command(char c){Serial.input.push_back(c);loop();}
void send(const char* s){while(*s) command(*s++);}
void wheels(int l,int r){assert(leftMotor.value==l);assert(rightMotor.value==r);}
int main(){
  setup(); wheels(90,90); assert(!moving);
  assert(leftMotor.pin==9 && rightMotor.pin==10);
  command('f'); wheels(78,102); // Raw firmware outputs; website corrects direction
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
  assert(Serial.reply=="NB3-DEMO-6 SERVO WATCHDOG=600 SPEED=12 FULL=90 LIMIT=NONE TRIM=24 LEFT=9 RIGHT=10");
''' + '\n'.join(checks) + '''
  // Every full-speed direction retains the independent disconnect timeout.
  for(char c : {'F','B','L','R','Q','E','Z','C'}){
    command('x'); nowMs=100; command(c); assert(moving);
    nowMs=701; loop(); wheels(90,90); assert(!moving);
  }
  // Full speed continues beyond two seconds while fresh commands arrive.
  command('x'); nowMs=1000; command('B'); wheels(180,0);
  for(nowMs=1100; nowMs<7100; nowMs+=100){
    command(nowMs % 200 ? 'B' : 'F'); assert(moving);
  }
  command('B'); wheels(180,0); assert(moving);
  nowMs+=599; loop(); wheels(180,0); assert(moving);
  nowMs+=2; loop(); wheels(90,90); assert(!moving);
  command('B'); wheels(180,0); assert(moving);
  command('x'); wheels(90,90); assert(!moving);
  // Calibrated packets retain the watchdog and reject malformed/range errors.
  nowMs=4000; send("@654E\\n"); wheels(101,78); assert(moving);
  nowMs=4601; loop(); wheels(90,90); assert(!moving);
  send("@004E\\n"); wheels(90,90); assert(!moving);
  send("@664EFFB\\n"); wheels(90,90); assert(!moving);
  send("@66"); command('x'); wheels(90,90); assert(!moving);
  send("@66"); nowMs+=51; loop(); wheels(90,90);
  send("4EFB\\n"); wheels(90,90); assert(!moving); // Discard stale suffix.
  send("@664E\\n"); wheels(102,78); assert(moving); // Valid new packet recovers.
  command('x'); wheels(90,90); assert(!moving);
}
''')
            subprocess.run(["g++", "-I", directory, str(root / "check.cpp"),
                            "-o", str(root / "check")], check=True, capture_output=True)
            subprocess.run([str(root / "check")], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
