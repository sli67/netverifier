import { createApp } from "./app.js";
import { config } from "./config.js";
import { pool } from "./db.js";

const server = createApp().listen(config.PORT, () => {
  console.log(`api listening on :${config.PORT}`);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    server.close(() => {
      void pool.end().then(() => process.exit(0));
    });
  });
}
