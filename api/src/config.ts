import { z } from "zod";

const schema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  PORT: z.coerce.number().int().default(3000),
  DATABASE_URL: z.string().min(1),
  SESSION_TTL_HOURS: z.coerce.number().positive().default(12),

  S3_BUCKET: z.string().min(1),
  S3_REGION: z.string().min(1),
  // Set only for an S3-compatible local store. Leave unset on AWS.
  S3_ENDPOINT: z.string().url().optional(),
  // Leave unset on AWS so the SDK uses its default credential chain.
  S3_ACCESS_KEY_ID: z.string().min(1).optional(),
  S3_SECRET_ACCESS_KEY: z.string().min(1).optional(),
});

export const config = schema.parse(process.env);
