import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assertCanonicalDigest,
  verifyCandidateAbsent,
  verifyImageMetadata,
  verifyPublishedImage,
} from "./openclaw-image-proof.mjs";

const REGISTRY_ID = "877352799272";
const REPOSITORY = "lightbulb/openclaw-extended";
const TAG = "2026.9.3-isol8.7-abc1234";
const BASE_DIGEST = `sha256:${"a".repeat(64)}`;
const TARGET_DIGEST = `sha256:${"b".repeat(64)}`;

const target = (overrides = {}) => ({
  registryId: REGISTRY_ID,
  repositoryName: REPOSITORY,
  tag: TAG,
  ...overrides,
});

const absentResponse = (tag = TAG) => ({
  images: [],
  failures: [{ failureCode: "ImageNotFound", imageId: { imageTag: tag } }],
});

const publishedResponse = (overrides = {}) => ({
  images: [
    {
      registryId: REGISTRY_ID,
      repositoryName: REPOSITORY,
      imageId: { imageTag: TAG, imageDigest: TARGET_DIGEST },
      ...overrides,
    },
  ],
  failures: [],
});

test("metadata proves the Docker base pin and derives the extended tag", () => {
  const metadata = {
    upstream: "ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7",
    extendedImage: `${REGISTRY_ID}.dkr.ecr.us-east-1.amazonaws.com/${REPOSITORY}`,
  };
  const dockerfile = [
    "FROM debian:bookworm-slim AS builder",
    "RUN true",
    `FROM ${metadata.upstream}@${BASE_DIGEST}`,
    "RUN true",
  ].join("\n");

  assert.deepEqual(verifyImageMetadata(metadata, dockerfile, "abc1234"), {
    image: metadata.extendedImage,
    tag: TAG,
    base_image: metadata.upstream,
    base_digest: BASE_DIGEST,
  });
});

for (const { name, metadata, dockerfile } of [
  {
    name: "tag/Docker mismatch",
    metadata: {
      upstream: "ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7",
      extendedImage: `${REGISTRY_ID}.dkr.ecr.us-east-1.amazonaws.com/${REPOSITORY}`,
    },
    dockerfile: `FROM ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.8@${BASE_DIGEST}\n`,
  },
  {
    name: "missing digest",
    metadata: {
      upstream: "ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7",
      extendedImage: `${REGISTRY_ID}.dkr.ecr.us-east-1.amazonaws.com/${REPOSITORY}`,
    },
    dockerfile: "FROM ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7\n",
  },
  {
    name: "wrong extended registry/repository",
    metadata: {
      upstream: "ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7",
      extendedImage: "000000000000.dkr.ecr.us-east-1.amazonaws.com/lightbulb/not-openclaw",
    },
    dockerfile: `FROM ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.7@${BASE_DIGEST}\n`,
  },
]) {
  test(`metadata rejects ${name}`, () => {
    assert.throws(
      () => verifyImageMetadata(metadata, dockerfile, "abc1234"),
      /Dockerfile base must exactly match metadata upstream with a canonical digest|Image metadata or source commit is invalid/,
    );
  });
}

test("preflight accepts only the exact tag's single ImageNotFound response", () => {
  assert.doesNotThrow(() => verifyCandidateAbsent(absentResponse(), target()));
});

for (const { name, response, query } of [
  { name: "existing image", response: publishedResponse(), query: target() },
  {
    name: "auth/API failure",
    response: { images: [], failures: [{ failureCode: "AccessDeniedException" }] },
    query: target(),
  },
  { name: "malformed absence", response: { images: [], failures: [] }, query: target() },
  {
    name: "duplicate failures",
    response: { images: [], failures: [...absentResponse().failures, ...absentResponse().failures] },
    query: target(),
  },
  { name: "wrong registry", response: absentResponse(), query: target({ registryId: "000000000000" }) },
  {
    name: "wrong repository",
    response: absentResponse(),
    query: target({ repositoryName: "lightbulb/not-openclaw" }),
  },
  { name: "wrong tag", response: absentResponse("other"), query: target() },
]) {
  test(`preflight rejects ${name}`, () => {
    assert.throws(
      () => verifyCandidateAbsent(response, query),
      /Candidate tag absence is unproven or tag already exists/,
    );
  });
}

test("post-push accepts one exact image with the converted OCI target digest", () => {
  assert.doesNotThrow(() =>
    verifyPublishedImage(publishedResponse(), { ...target(), expectedDigest: TARGET_DIGEST }),
  );
});

for (const { name, response, expectedDigest = TARGET_DIGEST } of [
  {
    name: "wrong post-push digest",
    response: publishedResponse({ imageId: { imageTag: TAG, imageDigest: BASE_DIGEST } }),
  },
  {
    name: "duplicate images",
    response: { images: [...publishedResponse().images, ...publishedResponse().images], failures: [] },
  },
  {
    name: "failure alongside image",
    response: { ...publishedResponse(), failures: absentResponse().failures },
  },
  { name: "wrong registry", response: publishedResponse({ registryId: "000000000000" }) },
  {
    name: "wrong repository",
    response: publishedResponse({ repositoryName: "lightbulb/not-openclaw" }),
  },
  {
    name: "wrong tag",
    response: publishedResponse({ imageId: { imageTag: "other", imageDigest: TARGET_DIGEST } }),
  },
  { name: "malformed expected digest", response: publishedResponse(), expectedDigest: "sha256:no" },
]) {
  test(`post-push rejects ${name}`, () => {
    assert.throws(
      () => verifyPublishedImage(response, { ...target(), expectedDigest }),
      /Published image identity or digest is unproven/,
    );
  });
}

test("canonical digest validation trims runner output but rejects non-sha256 values", () => {
  assert.equal(assertCanonicalDigest(` ${TARGET_DIGEST}\n`), TARGET_DIGEST);
  for (const value of ["", "sha256:no", "sha512:" + "a".repeat(128), TARGET_DIGEST.toUpperCase()]) {
    assert.throws(() => assertCanonicalDigest(value), /canonical sha256 digest/);
  }
});
