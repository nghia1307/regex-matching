FROM node:20-alpine

WORKDIR /app

# Dependencies first so a source edit does not reinstall the tree.
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev"]
