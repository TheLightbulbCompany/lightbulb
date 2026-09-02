import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QuestionCards } from "../QuestionCards";
import type { QuestionRecord } from "@lightbulb/shared";

let mockQuestions: QuestionRecord[] = [];
const answerMock = vi.fn();
const skipMock = vi.fn();

vi.mock("@/hooks/useQuestions", async (orig) => {
  const actual = await orig<typeof import("@/hooks/useQuestions")>();
  return {
    ...actual,
    useQuestionsContext: () => ({
      questions: mockQuestions,
      answer: answerMock,
      skip: skipMock,
      error: null,
    }),
  };
});

vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

function record(over: Partial<QuestionRecord> = {}): QuestionRecord {
  return {
    id: "q1",
    questions: [
      {
        questionId: "deploy_target",
        header: "Deploy",
        question: "Where should I deploy?",
        options: [{ label: "Vercel", description: "Zero config" }, { label: "Fly.io" }],
      },
    ],
    agentId: "agent_x",
    sessionKey: "agent:agent_x:main",
    createdAtMs: Date.now(),
    expiresAtMs: Date.now() + 900_000,
    status: "pending",
    ...over,
  };
}

describe("QuestionCards", () => {
  beforeEach(() => {
    answerMock.mockReset().mockResolvedValue(undefined);
    skipMock.mockReset().mockResolvedValue(undefined);
    mockQuestions = [];
  });

  it("renders nothing when there are no questions", () => {
    const { container } = render(<QuestionCards />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the header chip, the question text and every option", () => {
    mockQuestions = [record()];
    render(<QuestionCards />);
    expect(screen.getByText("Deploy")).toBeInTheDocument();
    expect(screen.getByText("Where should I deploy?")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Vercel/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Fly\.io/ })).toBeInTheDocument();
    expect(screen.getByText("Zero config")).toBeInTheDocument();
  });

  it("labels the card as a region so a screen reader can find it", () => {
    mockQuestions = [record()];
    render(<QuestionCards />);
    expect(screen.getByRole("group", { name: "Agent question: Deploy" })).toBeInTheDocument();
  });

  it("single question, single select — one click answers", () => {
    mockQuestions = [record()];
    render(<QuestionCards />);
    fireEvent.click(screen.getByRole("radio", { name: /Vercel/ }));
    expect(answerMock).toHaveBeenCalledWith("q1", { deploy_target: ["Vercel"] }, undefined);
  });

  it("multi-select collects choices and submits them together", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "stack",
            header: "Stack",
            question: "Pick your tools.",
            options: [{ label: "Next" }, { label: "Expo" }],
            multiSelect: true,
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Next/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Expo/ }));
    expect(answerMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith("q1", { stack: ["Next", "Expo"] }, undefined);
  });

  it("single-select toggling replaces the choice rather than accumulating", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "deploy_target",
            header: "Deploy",
            question: "Where?",
            options: [{ label: "Vercel" }, { label: "Fly.io" }],
            // isOther keeps the Send button so the click doesn't auto-submit.
            isOther: true,
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    fireEvent.click(screen.getByRole("radio", { name: /Vercel/ }));
    fireEvent.click(screen.getByRole("radio", { name: /Fly\.io/ }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith("q1", { deploy_target: ["Fly.io"] }, undefined);
  });

  it("isOther appends the free text after the selected labels", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "deploy_target",
            header: "Deploy",
            question: "Where should I deploy?",
            options: [{ label: "Vercel" }],
            isOther: true,
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    fireEvent.change(screen.getByLabelText("Other"), {
      target: { value: "  bare metal  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith("q1", { deploy_target: ["bare metal"] }, undefined);
  });

  it("a question with no options renders a free-text field", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "repo",
            header: "Repo",
            question: "Which repo?",
            options: [],
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    const input = screen.getByLabelText("Which repo?");
    expect(input).toHaveAttribute("type", "text");
    fireEvent.change(input, { target: { value: "lightbulb" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith("q1", { repo: ["lightbulb"] }, undefined);
  });

  it("a secret question renders a password input and forwards the allowed hosts", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "stripe_key",
            header: "Stripe",
            question: "Paste your Stripe API key.",
            options: [],
            isSecret: true,
            secretStore: {
              name: "STRIPE_API_KEY",
              kind: "secret",
              allowedHosts: ["api.stripe.com"],
              reason: "Needed to read your charges.",
            },
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    const input = screen.getByLabelText("Paste your Stripe API key.");
    expect(input).toHaveAttribute("type", "password");
    // Consent surface: what it is stored as, why, and where it may be sent.
    expect(screen.getByText("STRIPE_API_KEY")).toBeInTheDocument();
    expect(screen.getByText(/Needed to read your charges\./)).toBeInTheDocument();
    expect(screen.getByText(/api\.stripe\.com/)).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "sk_live_x" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith(
      "q1",
      { stripe_key: ["sk_live_x"] },
      ["api.stripe.com"],
    );
  });

  it("secret values are not trimmed", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "stripe_key",
            header: "Stripe",
            question: "Paste your Stripe API key.",
            options: [],
            isSecret: true,
            secretStore: { name: "STRIPE_API_KEY", kind: "secret" },
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    fireEvent.change(screen.getByLabelText("Paste your Stripe API key."), {
      target: { value: " sk_live_x " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(answerMock).toHaveBeenCalledWith("q1", { stripe_key: [" sk_live_x "] }, undefined);
  });

  it("Skip cancels the request", () => {
    mockQuestions = [record()];
    render(<QuestionCards />);
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(skipMock).toHaveBeenCalledWith("q1");
  });

  it("Send stays disabled until every question has an answer", () => {
    mockQuestions = [
      record({
        questions: [
          {
            questionId: "a",
            header: "A",
            question: "First?",
            options: [{ label: "yes" }, { label: "no" }],
          },
          {
            questionId: "b",
            header: "B",
            question: "Second?",
            options: [{ label: "yes" }, { label: "no" }],
          },
        ],
      }),
    ];
    render(<QuestionCards />);
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    fireEvent.click(screen.getAllByRole("radio", { name: /yes/ })[0]);
    expect(send).toBeDisabled();
    fireEvent.click(screen.getAllByRole("radio", { name: /no/ })[1]);
    expect(send).toBeEnabled();
    fireEvent.click(send);
    expect(answerMock).toHaveBeenCalledWith("q1", { a: ["yes"], b: ["no"] }, undefined);
  });

  it("shows a countdown to the request's expiry", () => {
    mockQuestions = [record({ expiresAtMs: Date.now() + 125_000 })];
    render(<QuestionCards />);
    expect(screen.getByText(/2:0\d/)).toBeInTheDocument();
  });

  it("disables the controls while a submission is in flight", async () => {
    let release: () => void = () => {};
    answerMock.mockImplementation(
      () => new Promise<void>((resolve) => (release = () => resolve())),
    );
    mockQuestions = [record()];
    render(<QuestionCards />);
    fireEvent.click(screen.getByRole("radio", { name: /Vercel/ }));
    expect(screen.getByRole("button", { name: "Skip" })).toBeDisabled();
    release();
  });
});
