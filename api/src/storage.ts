import { DeleteObjectsCommand, GetObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { config } from "./config.js";

const s3 = new S3Client({
  region: config.S3_REGION,
  ...(config.S3_ENDPOINT ? { endpoint: config.S3_ENDPOINT, forcePathStyle: true } : {}),
  ...(config.S3_ACCESS_KEY_ID && config.S3_SECRET_ACCESS_KEY
    ? {
        credentials: {
          accessKeyId: config.S3_ACCESS_KEY_ID,
          secretAccessKey: config.S3_SECRET_ACCESS_KEY,
        },
      }
    : {}),
});

export async function putText(key: string, body: string): Promise<void> {
  await s3.send(
    new PutObjectCommand({
      Bucket: config.S3_BUCKET,
      Key: key,
      Body: body,
      ContentType: "text/plain; charset=utf-8",
    }),
  );
}

export async function getText(key: string): Promise<string> {
  const { Body } = await s3.send(new GetObjectCommand({ Bucket: config.S3_BUCKET, Key: key }));
  if (!Body) throw new Error(`empty object body for ${key}`);
  return Body.transformToString("utf-8");
}

/** Deletes the given objects. Keys that do not exist are ignored. */
export async function deleteObjects(keys: string[]): Promise<void> {
  // The API accepts at most 1000 keys per request.
  for (let i = 0; i < keys.length; i += 1000) {
    const result = await s3.send(
      new DeleteObjectsCommand({
        Bucket: config.S3_BUCKET,
        Delete: { Objects: keys.slice(i, i + 1000).map((Key) => ({ Key })), Quiet: true },
      }),
    );
    if (result.Errors?.length) {
      throw new Error(`failed to delete ${result.Errors.length} object(s), first: ${result.Errors[0]?.Key}`);
    }
  }
}
