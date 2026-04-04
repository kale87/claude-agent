FROM node:20-alpine

# Create app directory
WORKDIR /app

# Install dependencies first (better layer caching)
COPY package*.json ./
RUN npm install --omit=dev

# Copy source
COPY server.js .

# Expose port
EXPOSE 3000

# Start the server
CMD ["node", "server.js"]
