# S11 web 静态镜像：Node 22 构建产物 + nginx 只读运行（specs/s11 §2 非 root/health）。
# 构建阶段与运行阶段都 digest pin；npm ci 只装 lockfile 版本。
FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS build

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf

# 非 root（nginx 用户 101/101），只读 rootfs 由 compose tmpfs 补齐可写路径
USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]

CMD ["nginx", "-g", "daemon off;"]
