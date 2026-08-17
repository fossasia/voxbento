// Simple test runner for the TTS Parser
const fs = require('fs');

// Create a mock window environment
global.window = {};

// Load the script
const scriptContent = fs.readFileSync('static/js/tts-parser.js', 'utf-8');
eval(scriptContent);

const parser = global.window.TTSParser;

let passed = 0;
let failed = 0;

function assert(condition, message) {
    if (condition) {
        passed++;
    } else {
        console.error("FAIL:", message);
        failed++;
    }
}

function assertThrows(fn, errorMessagePrefix) {
    try {
        fn();
        console.error("FAIL: Expected function to throw, but it didn't");
        failed++;
    } catch (e) {
        if (e.message.startsWith(errorMessagePrefix)) {
            passed++;
        } else {
            console.error(`FAIL: Expected error starting with '${errorMessagePrefix}', got '${e.message}'`);
            failed++;
        }
    }
}

console.log("Running TTS Parser tests...");

// Test 1: Frame too short
assertThrows(() => {
    parser.parseFrame(new ArrayBuffer(4));
}, "Frame too short");

// Test 2: Unsupported version
const badVersionBuf = new ArrayBuffer(5);
new DataView(badVersionBuf).setUint8(0, 2); // Version 2
assertThrows(() => {
    parser.parseFrame(badVersionBuf);
}, "Unsupported protocol version");

// Test 3: Truncated frame
const truncatedBuf = new ArrayBuffer(5);
new DataView(truncatedBuf).setUint8(0, 1);
new DataView(truncatedBuf).setUint32(1, 10, false); // Length 10, but buffer ends at 5
assertThrows(() => {
    parser.parseFrame(truncatedBuf);
}, "Frame truncated");

// Test 4: Valid frame parsing
const jsonStr = JSON.stringify({ segment_id: "test-123", seq: 1 });
const jsonBytes = Buffer.from(jsonStr, 'utf-8');
const audioBytes = Buffer.from("fakeaudio");

const validBuf = new ArrayBuffer(5 + jsonBytes.length + audioBytes.length);
const view = new DataView(validBuf);

view.setUint8(0, 1); // Version 1
view.setUint32(1, jsonBytes.length, false); // length in big-endian

const u8 = new Uint8Array(validBuf);
u8.set(jsonBytes, 5);
u8.set(audioBytes, 5 + jsonBytes.length);

const parsed = parser.parseFrame(validBuf);
assert(parsed.header.segment_id === "test-123", "Parsed segment_id mismatch");
assert(parsed.header.seq === 1, "Parsed seq mismatch");
assert(parsed.audioBytes.length === audioBytes.length, "Parsed audio bytes length mismatch");

const parsedAudioStr = Buffer.from(parsed.audioBytes).toString('utf-8');
assert(parsedAudioStr === "fakeaudio", "Parsed audio bytes content mismatch");

console.log(`\nTests completed: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
