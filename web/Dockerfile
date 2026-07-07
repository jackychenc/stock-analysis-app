FROM node:22-alpine AS build
WORKDIR /srv/web
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /srv/web
COPY --from=build /srv/web/.next/standalone ./
COPY --from=build /srv/web/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
