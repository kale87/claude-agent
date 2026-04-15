FROM node:20-alpine

WORKDIR /app

# Install build tools required by better-sqlite3 (native module)
# Removed after install to keep the image lean
COPY package*.json ./
RUN apk add --no-cache --virtual .build-deps python3 make g++ \
  && npm install --omit=dev \
  && apk del .build-deps

# Copy both server and public folder
COPY server.js .
COPY public ./public

EXPOSE 3000

CMD ["node", "server.js"]
