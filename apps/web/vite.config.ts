import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// S1-T6：同源 Vite dev server（5173），前端经相对路径调用 /api/v1/* 与 /auth/*
// （Vite proxy 转发到后端 create_app，生产同源部署同款）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // 本地 e2e：后端 CSRF 门禁要求 Origin == scheme://netloc（同源部署语义），
        // 代理必须同时改写 Host（changeOrigin）与 Origin，等价生产反向代理管道。
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:8000" },
      },
      "/auth": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:8000" },
      },
      "/scim": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:8000" },
      },
    },
  },
});
