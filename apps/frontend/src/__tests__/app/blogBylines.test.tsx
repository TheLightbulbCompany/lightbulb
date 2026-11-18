import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import BlogPostPage from "@/app/blog/[slug]/page";
import { getAllPosts } from "@/content/blog";

const FOUNDERS = ["Prasiddha Parthsarthy", "Leo", "Aden"];

describe("blog posts", () => {
  it("are signed by a founder and carry no internal names or off-voice words", () => {
    for (const post of getAllPosts()) {
      expect(FOUNDERS.some((f) => post.author.startsWith(f)), post.slug).toBe(true);
      expect(post.tags.join(" "), post.slug).not.toMatch(/paperclip|hermes|agentcore/i);
      expect(`${post.title} ${post.description} ${post.content}`, post.slug).not.toMatch(/ai-native|paperclip/i);
    }
  });

  // No posts since the 2026-11 move to the receptionist; runs again with the first one.
  it.skipIf(getAllPosts().length === 0)("end with the sign-up door, tagged with the post", async () => {
    const post = getAllPosts()[0];
    render(await BlogPostPage({ params: Promise.resolve({ slug: post.slug }) }));
    expect(screen.getByRole("link", { name: /Set up your assistant/ })).toHaveAttribute("href", `/sign-up?source=blog-${post.slug}`);
    expect(screen.getByText(`By ${post.author}`)).toBeInTheDocument();
  });
});
