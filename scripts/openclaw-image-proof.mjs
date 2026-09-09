#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const ECR_REGISTRY_ID = "877352799272";
export const ECR_REPOSITORY = "lightbulb/openclaw-extended";
export const ECR_IMAGE = `${ECR_REGISTRY_ID}.dkr.ecr.us-east-1.amazonaws.com/${ECR_REPOSITORY}`;

const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const ABSENCE_ERROR = "Candidate tag absence is unproven or tag already exists";
const IMMUTABILITY_ERROR = "ECR repository immutability is unproven";
const PUBLISHED_ERROR = "Published image identity or digest is unproven";

export function assertCanonicalDigest(value) {
  const digest = typeof value === "string" ? value.trim() : "";
  if (!DIGEST_RE.test(digest)) {
    throw new Error("Expected a canonical sha256 digest");
  }
  return digest;
}

export function verifyImageMetadata(metadata, dockerfile, shortSha) {
  const upstream = typeof metadata?.upstream === "string" ? metadata.upstream : "";
  const image = typeof metadata?.extendedImage === "string" ? metadata.extendedImage : "";
  const from = [...dockerfile.matchAll(/^FROM\s+(\S+)/gm)].at(-1)?.[1] ?? "";
  const [dockerBase, dockerDigest, extra] = from.split("@");

  if (!upstream || dockerBase !== upstream || extra !== undefined) {
    throw new Error("Dockerfile base must exactly match metadata upstream with a canonical digest");
  }
  let baseDigest;
  try {
    baseDigest = assertCanonicalDigest(dockerDigest);
  } catch {
    throw new Error("Dockerfile base must exactly match metadata upstream with a canonical digest");
  }
  if (image !== ECR_IMAGE || typeof shortSha !== "string" || !/^[0-9a-f]+$/.test(shortSha)) {
    throw new Error("Image metadata or source commit is invalid");
  }
  const separator = upstream.lastIndexOf(":");
  const upstreamTag = separator > upstream.lastIndexOf("/") ? upstream.slice(separator + 1) : "";
  if (!upstreamTag) {
    throw new Error("Image metadata or source commit is invalid");
  }
  return {
    image,
    tag: `${upstreamTag.replace(/^v/, "")}-${shortSha}`,
    base_image: upstream,
    base_digest: baseDigest,
  };
}

function hasExpectedRepository({ registryId, repositoryName } = {}) {
  return registryId === ECR_REGISTRY_ID && repositoryName === ECR_REPOSITORY;
}

function hasExpectedTarget(query) {
  return (
    hasExpectedRepository(query) &&
    typeof query?.tag === "string" &&
    query.tag.length > 0
  );
}

export function verifyImmutableRepository(response, query) {
  const repository = response?.repositories?.[0];
  const exclusions = repository?.imageTagMutabilityExclusionFilters;
  const immutable =
    hasExpectedRepository(query) &&
    Array.isArray(response?.repositories) &&
    response.repositories.length === 1 &&
    repository?.registryId === query.registryId &&
    repository?.repositoryName === query.repositoryName &&
    repository?.imageTagMutability === "IMMUTABLE" &&
    (exclusions === undefined || (Array.isArray(exclusions) && exclusions.length === 0));
  if (!immutable) {
    throw new Error(IMMUTABILITY_ERROR);
  }
}

export function verifyCandidateAbsent(response, query) {
  const tag = query?.tag;
  const absent =
    Array.isArray(response?.images) &&
    Array.isArray(response?.failures) &&
    response?.images?.length === 0 &&
    response?.failures?.length === 1 &&
    response.failures[0].failureCode === "ImageNotFound" &&
    response.failures[0].imageId?.imageTag === tag;
  if (!hasExpectedTarget(query) || !absent) {
    throw new Error(ABSENCE_ERROR);
  }
}

export function verifyPublishedImage(response, query) {
  let expectedDigest;
  try {
    expectedDigest = assertCanonicalDigest(query?.expectedDigest);
  } catch {
    throw new Error(PUBLISHED_ERROR);
  }
  const image = response?.images?.[0];
  const proven =
    hasExpectedTarget(query) &&
    Array.isArray(response?.images) &&
    Array.isArray(response?.failures) &&
    response?.images?.length === 1 &&
    response?.failures?.length === 0 &&
    image?.registryId === query.registryId &&
    image?.repositoryName === query.repositoryName &&
    image?.imageId?.imageTag === query.tag &&
    image?.imageId?.imageDigest === expectedDigest;
  if (!proven) {
    throw new Error(PUBLISHED_ERROR);
  }
}

function readResponse() {
  return JSON.parse(readFileSync(0, "utf8"));
}

function main([command, ...args]) {
  if (command === "metadata" && args.length === 1) {
    const result = verifyImageMetadata(
      JSON.parse(readFileSync("openclaw-version.json", "utf8")),
      readFileSync("apps/infra/openclaw/Dockerfile", "utf8"),
      args[0],
    );
    for (const [key, value] of Object.entries(result)) console.log(`${key}=${value}`);
    return;
  }
  if (command === "preflight" && args.length === 3) {
    verifyCandidateAbsent(readResponse(), {
      registryId: args[0],
      repositoryName: args[1],
      tag: args[2],
    });
    return;
  }
  if (command === "repository" && args.length === 2) {
    verifyImmutableRepository(readResponse(), {
      registryId: args[0],
      repositoryName: args[1],
    });
    return;
  }
  if (command === "post-push" && args.length === 4) {
    verifyPublishedImage(readResponse(), {
      registryId: args[0],
      repositoryName: args[1],
      tag: args[2],
      expectedDigest: args[3],
    });
    return;
  }
  if (command === "digest" && args.length === 1) {
    const expected = assertCanonicalDigest(args[0]);
    if (assertCanonicalDigest(readFileSync(0, "utf8")) !== expected) {
      throw new Error("Resolved digest does not match the pinned digest");
    }
    return;
  }
  if (command === "canonical-digest" && args.length === 0) {
    console.log(assertCanonicalDigest(readFileSync(0, "utf8")));
    return;
  }
  throw new Error(
    "Usage: openclaw-image-proof.mjs metadata|repository|preflight|post-push|digest|canonical-digest",
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
