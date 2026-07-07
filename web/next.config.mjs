/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    // Local dev: proxy API calls to the FastAPI container/process.
    const api = process.env.API_BASE_URL ?? "http://localhost:8000/api/v1";
    return [{ source: "/api/v1/:path*", destination: `${api}/:path*` }];
  },
};

export default nextConfig;
