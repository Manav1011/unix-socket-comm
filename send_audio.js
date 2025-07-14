const net = require("net");
const fs = require("fs");
const path = "/tmp/audio.sock";

// Create a client socket
const client = net.createConnection({ path }, () => {
  console.log("Connected to Python server");
});

// Handle responses from Python
client.on("data", (data) => {
  console.log("Python responded:", data.toString().trim());
});

// Send audio data every second
setInterval(() => {
  const audio = fs.readFileSync("audio.wav"); // or any binary audio buffer
  const lengthBuffer = Buffer.alloc(4);
  lengthBuffer.writeUInt32BE(audio.length);
  client.write(lengthBuffer); // send length first
  client.write(audio);        // then send audio binary
}, 1000);
