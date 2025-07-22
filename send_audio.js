const net = require("net");
const fs = require("fs");
const path = "/tmp/audio.sock";

// Create a client socket
const client = net.createConnection({ path }, () => {
  console.log("Connected to Python server");
  sendPing();
  setInterval(sendAudio, 5000); // Send audio every 5 seconds
});

// Buffer for incoming data
let buffer = Buffer.alloc(0);

// Handle responses from Python
client.on("data", (data) => {
  buffer = Buffer.concat([buffer, data]);
  while (buffer.length >= 4) {
    const msgLen = buffer.readUInt32BE(0);
    if (buffer.length < 4 + msgLen) break;
    const msgBuf = buffer.slice(4, 4 + msgLen);
    const msg = JSON.parse(msgBuf.toString());
    console.log("Python responded:", msg);
    buffer = buffer.slice(4 + msgLen);
    // Send next ping after receiving pong
    if (msg.type === "pong") {
      setTimeout(sendPing, 1000);
    }
  }
});

function sendPing() {
  const ping = { type: "ping", msg: "Ping from Node.js! YOOOOO" };
  const pingBuf = Buffer.from(JSON.stringify(ping));
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(pingBuf.length);
  client.write(Buffer.concat([lenBuf, pingBuf]));
}

function sendAudio() {
  fs.readFile("audio.wav", (err, audioBuf) => {
    if (err) {
      console.error("Failed to read audio.wav:", err);
      return;
    }
    const header = {
      type: "audio",
      audio_length: audioBuf.length,
      filename: "audio.wav"
    };
    const headerBuf = Buffer.from(JSON.stringify(header));
    const headerLenBuf = Buffer.alloc(4);
    headerLenBuf.writeUInt32BE(headerBuf.length);
    // Send: [4 bytes header length][header][audio]
    client.write(Buffer.concat([headerLenBuf, headerBuf, audioBuf]));
    console.log("Sent audio.wav (", audioBuf.length, "bytes)");
  });
}
