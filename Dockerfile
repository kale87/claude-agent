FROM node:20-alpine

# Install git so the git routes work
RUN apk add --no-cache git

WORKDIR /app

COPY package*.json ./
RUN npm install --omit=dev

COPY server.js .
COPY public ./public

EXPOSE 3000

CMD ["node", "server.js"]
