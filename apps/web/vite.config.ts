import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// S1-T6：同源 Vite dev server（5173），前端经相对路径调用 /api/v1/* 与 /auth/*
// （Vite proxy 转发到后端 create_app，生产同源部署同款）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/auth": "http://127.0.0.1:8000",
      "/scim": "http://127.0.0.1:8000",
    },
  },
});
