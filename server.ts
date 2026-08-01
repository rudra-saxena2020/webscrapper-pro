import express from "express";
import http from "http";
import { createServer as createViteServer } from "vite";
import path from "path";
import { fileURLToPath } from "url";
import axios from "axios";
import * as cheerio from "cheerio";
import { GoogleGenAI } from "@google/genai";
import dotenv from "dotenv";

dotenv.config({ override: false });

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PYTHON_BACKEND = "http://127.0.0.1:8000";

import fs from "fs";

function killPort(port: number): void {
  // On Windows, /proc does not exist. We return early to prevent errors.
  if (process.platform === "win32") return;

  const hexPort = port.toString(16).toUpperCase().padStart(4, "0");
  for (const proto of ["tcp", "tcp6"]) {
    try {
      const lines = fs.readFileSync(`/proc/net/${proto}`, "utf8").split("\n");
      for (const line of lines) {
        const parts = line.trim().split(/\s+/);
        if (parts.length < 10) continue;
        const localPort = parts[1]?.split(":")[1]?.toUpperCase();
        const state = parts[3];
        if (localPort === hexPort && state === "0A") {
          const inode = parts[9];
          // Find PID owning this inode
          for (const pid of fs.readdirSync("/proc")) {
            if (!/^\d+$/.test(pid) || pid === String(process.pid)) continue;
            try {
              for (const fd of fs.readdirSync(`/proc/${pid}/fd`)) {
                try {
                  const link = fs.readlinkSync(`/proc/${pid}/fd/${fd}`);
                  if (link.includes(`socket:[${inode}]`)) {
                    process.kill(Number(pid), "SIGKILL");
                    return;
                  }
                } catch (_) {}
              }
            } catch (_) {}
          }
        }
      }
    } catch (_) {}
  }
}

async function startServer() {
  killPort(5000);
  await new Promise(r => setTimeout(r, 500));
  const app = express();
  const PORT = 5000;

  app.use(express.json({ limit: "50mb" }));

  // ── Node.js Gemini scraper — must be registered BEFORE the proxy catch-all ──
  app.post("/api/scrape_url", async (req, res) => {
    const { url } = req.body;
    if (!url) {
      return res.status(400).json({ error: "URL is required" });
    }

    try {
      console.log(`Scraping URL: ${url}`);

      const apiKey = process.env.GEMINI_API_KEY || process.env.API_KEY;
      if (!apiKey || apiKey === "MY_GEMINI_API_KEY") {
        throw new Error(
          "Gemini API key is not configured. Please set GEMINI_API_KEY in the Secrets panel."
        );
      }
      const genAI = new GoogleGenAI({ apiKey });

      const response = await axios.get(url, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        },
      });

      const $ = cheerio.load(response.data);
      const pageTitle = $("title").text();
      const bodyText = $("body").text().slice(0, 20000);

      const model = "gemini-1.5-flash";
      const prompt = `
        Extract product information from the following text content of a fashion website.
        The output must be a JSON array of products, where each product has:
        - Title
        - Body (HTML) (a brief description)
        - Vendor (Brand)
        - Type (Category)
        - Tags (comma-separated)
        - variants: a list of variants, each with:
          - Variant SKU (generate a unique one if not found)
          - size (if available, otherwise "One Size")
          - Variant Price (numeric string)
          - Variant Compare At Price (if available)
          - images: a list of image URLs

        Text content:
        Title: ${pageTitle}
        Body: ${bodyText}

        Return ONLY the JSON array.
      `;

      try {
        const result = await genAI.models.generateContent({
          model,
          contents: [{ role: "user", parts: [{ text: prompt }] }],
          config: { responseMimeType: "application/json" },
        });
        const products = JSON.parse(result.text ?? "[]");
        res.json({ products });
      } catch (error: any) {
        console.error("Gemini API error:", error.message);
        return res.json({ products: [] });
      }
    } catch (error: any) {
      console.error("Scraping error:", error);
      res.status(500).json({ error: error.message || "Failed to scrape URL" });
    }
  });

  // ── Binary passthrough for file downloads (must come BEFORE the json proxy) ──
  app.get("/api/download/:scraperId", async (req, res) => {
    const targetUrl = `${PYTHON_BACKEND}${req.path}`;
    try {
      const response = await axios({
        method: "GET",
        url: targetUrl,
        params: req.query,
        responseType: "stream",
        timeout: 60000,
        validateStatus: () => true,
      });
      // Forward Flask's headers verbatim so the browser gets the right filename
      const forward = ["content-type", "content-disposition", "content-length", "cache-control"];
      forward.forEach((h) => {
        const val = response.headers[h];
        if (val) res.setHeader(h, val);
      });
      // Override to guarantee correct type/name even if Flask omits them
      const scraperId = req.params.scraperId;
      res.setHeader("Content-Type", "text/csv; charset=utf-8");
      res.setHeader("Content-Disposition", `attachment; filename="${scraperId}_products_shopify.csv"`);
      res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
      res.status(response.status);
      (response.data as NodeJS.ReadableStream).pipe(res);
    } catch (error: any) {
      res.status(502).json({ error: "Download proxy error", details: error.message });
    }
  });

  // ── Proxy all other /api/* requests to the Python Flask backend ──
  app.all("/api/*", async (req, res) => {
    const targetUrl = `${PYTHON_BACKEND}${req.path}`;
    console.log(`[Proxy] ${req.method} ${req.path} -> ${targetUrl}`);
    try {
      const response = await axios({
        method: req.method as any,
        url: targetUrl,
        data: req.body,
        params: req.query,
        headers: {
          "content-type": req.headers["content-type"] || "application/json",
          // Forward multi-store headers so Flask can read them
          ...(req.headers["x-store-key"] ? { "x-store-key": req.headers["x-store-key"] } : {}),
          ...(req.headers["x-confirm-main"] ? { "x-confirm-main": req.headers["x-confirm-main"] } : {}),
        },
        timeout: 30000,
        validateStatus: () => true,
      });
      console.log(`[Proxy] Success: ${response.status}`);
      res.status(response.status).json(response.data);
    } catch (error: any) {
      console.error(`[Proxy] Error: ${error.message} (code: ${error.code})`);
      if (error.code === "ECONNREFUSED" || error.code === "ENOTFOUND") {
        res
          .status(503)
          .json({ error: "Python backend offline", code: "BACKEND_OFFLINE" });
      } else {
        res
          .status(502)
          .json({ error: "Proxy error", details: error.message });
      }
    }
  });

  // Create a single HTTP server so Vite's HMR WebSocket shares it
  // instead of trying to bind port 24678 independently.
  const httpServer = http.createServer(app);

  // ── Vite middleware for development ──
  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: {
        middlewareMode: true,
        hmr: { server: httpServer },
      },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), "dist");
    app.use(express.static(distPath));
    app.get("*", (req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
  }

  // Retry-aware listen helper
  async function listenRetry(server: http.Server, port: number, maxAttempts = 8): Promise<void> {
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      await new Promise<void>((resolve, reject) => {
        const onError = (err: NodeJS.ErrnoException) => {
          server.removeListener("error", onError);
          reject(err);
        };
        server.once("error", onError);
        server.listen(port, "0.0.0.0", () => {
          server.removeListener("error", onError);
          resolve();
        });
      }).catch(async (err: NodeJS.ErrnoException) => {
        if (err.code === "EADDRINUSE" && attempt < maxAttempts) {
          console.log(`Port ${port} still busy (attempt ${attempt}/${maxAttempts}), killing and retrying...`);
          killPort(port);
          await new Promise(r => setTimeout(r, 1500));
        } else {
          throw err;
        }
      });
      // If we got here without throwing, we're listening
      if (server.listening) break;
    }
  }

  // Primary port — Replit preview pane
  await listenRetry(httpServer, PORT);
  console.log(`Server running on port ${PORT}`);

  // Single port serving both HTTP and Vite HMR in development
}

startServer();
