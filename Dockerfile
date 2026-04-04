FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install --omit=dev

# Copy both server and public folder
COPY server.js .
COPY public ./public

EXPOSE 3000

CMD ["node", "server.js"]
