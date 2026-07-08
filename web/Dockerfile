FROM node:22-alpine AS build
WORKDIR /srv/web
COPY package.json ./
RUN npm install
COPY . .
# Next.js bakes rewrite destinations at BUILD time (routes-manifest), so the
# in-cluster API target must be set here — runtime env cannot change it.
ENV API_BASE_URL=http://api:8000/api/v1
RUN npm run build

FROM node:22-alpine
WORKDIR /srv/web
COPY --from=build /srv/web/.next/standalone ./
COPY --from=build /srv/web/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
