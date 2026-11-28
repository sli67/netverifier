# Build from the repo root: docker build -f api/Dockerfile .
FROM node:22-slim AS build
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN npm install -g corepack@latest && corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY api/package.json api/
RUN pnpm install --frozen-lockfile --filter verifier-api
COPY api/ api/
RUN pnpm -C api build

FROM node:22-slim
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN npm install -g corepack@latest && corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY api/package.json api/
RUN pnpm install --frozen-lockfile --prod --filter verifier-api
COPY --from=build /app/api/dist api/dist
COPY api/migrations api/migrations
# Lets DATABASE_URL verify the RDS certificate (sslrootcert=/etc/ssl/rds-global-bundle.pem)
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds-global-bundle.pem
RUN chmod 644 /etc/ssl/rds-global-bundle.pem
WORKDIR /app/api
USER node
EXPOSE 3000
CMD ["node", "dist/server.js"]
