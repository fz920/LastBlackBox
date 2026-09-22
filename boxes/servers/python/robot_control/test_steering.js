"use strict";

function testSteering(SteeringInput) {
  let assertions = 0;
  const check = (input, expected) => {
    if (input.command !== expected) throw new Error(`Expected ${expected}, got ${input.command}`);
    assertions++;
  };
  // Every combination, including opposite directions and all four held.
  const expected = ["stop", "forward", "backward", "stop", "left", "forward_left",
    "backward_left", "left", "right", "forward_right", "backward_right", "right",
    "stop", "forward", "backward", "stop"];
  for (let mask = 0; mask < 16; mask++) {
    for (const names of [["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"], ["w", "s", "a", "d"]]) {
      const input = new SteeringInput();
      names.forEach((key, i) => {if (mask & (1 << i)) input.keys.add(key);});
      check(input, expected[mask]);
    }
  }
  const input = new SteeringInput();
  input.keys.add("ArrowUp"); check(input, "forward");
  input.keys.add("ArrowRight"); check(input, "forward_right");
  input.keys.delete("ArrowRight"); check(input, "forward");
  input.keys.add("w");
  input.keys.delete("ArrowUp"); check(input, "forward");
  input.pointers.set(1, "right"); check(input, "forward_right");
  input.keys.clear(); check(input, "right");
  input.pointers.set(2, "forward"); check(input, "forward_right");
  input.pointers.set(3, "forward");
  input.pointers.delete(2); check(input, "forward_right");
  input.pointers.delete(1); check(input, "forward");
  input.clear(); check(input, "stop");
  return `${assertions} steering assertions passed`;
}

if (typeof module !== "undefined") {
  module.exports = testSteering;
  if (typeof require !== "undefined" && require.main === module) {
    console.log(testSteering(require("./site/steering.js")));
  }
}
