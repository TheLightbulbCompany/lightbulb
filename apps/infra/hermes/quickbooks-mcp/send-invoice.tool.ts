import { sendQuickbooksInvoice } from "../handlers/send-quickbooks-invoice.handler.js";
import { ToolDefinition } from "../types/tool-definition.js";
import { z } from "zod";

const toolName = "send_invoice";
const toolDescription =
  "Email an existing QuickBooks Online invoice to the customer. QuickBooks sends its own " +
  "invoice email (PDF attached, with a Pay link when payments are enabled) to send_to, or to " +
  "the invoice's BillEmail address when send_to is omitted, and marks the invoice " +
  "EmailStatus=EmailSent. Create or update the invoice first; sending does not change it.";

const toolSchema = z.object({
  invoice_id: z.string().min(1, { message: "Invoice ID is required" }),
  send_to: z
    .string()
    .email()
    .optional()
    .describe("Email address to send the invoice to. Omit to use the invoice's BillEmail."),
});

const toolHandler = async ({ params }: any) => {
  const { invoice_id, send_to } = params;
  const response = await sendQuickbooksInvoice(invoice_id, send_to);

  if (response.isError) {
    return {
      content: [
        {
          type: "text" as const,
          text: `Error sending invoice ${invoice_id}: ${response.error}`,
        },
      ],
    };
  }

  const invoice = response.result || {};
  const to = invoice.BillEmail?.Address ?? send_to ?? "the invoice's BillEmail address";
  return {
    content: [
      {
        type: "text" as const,
        text: `Invoice ${invoice_id} sent to ${to}. EmailStatus: ${invoice.EmailStatus ?? "unknown"}.`,
      },
    ],
  };
};

export const SendInvoiceTool: ToolDefinition<typeof toolSchema> = {
  name: toolName,
  description: toolDescription,
  schema: toolSchema,
  handler: toolHandler,
};
