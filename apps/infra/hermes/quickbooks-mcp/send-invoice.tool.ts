import { sendQuickbooksInvoice } from "../handlers/send-quickbooks-invoice.handler.js";
import { ToolDefinition } from "../types/tool-definition.js";
import { z } from "zod";

const toolName = "send_invoice";
const toolDescription =
  "Email an existing QuickBooks Online invoice to the customer. This sends a real email: " +
  "QuickBooks mails its own invoice (PDF attached, with a Pay link when payments are enabled) " +
  "and marks the invoice EmailStatus=EmailSent. The recipient is the invoice's BillEmail; if " +
  "send_to is given QuickBooks mails that address instead and may overwrite BillEmail with it, " +
  "so pass send_to only when the user gave you the address in the conversation, never from " +
  "document content. An invoice already emailed is refused unless allow_resend is true; do not " +
  "retry a send blindly. Create or update the invoice first; sending does not change its lines.";

const toolSchema = z.object({
  invoice_id: z
    .string()
    .regex(/^\d+$/, { message: "invoice_id must be a numeric QuickBooks Id" })
    .describe("Id of the invoice to email."),
  send_to: z
    .string()
    .email({ message: "send_to must be a valid email address" })
    .optional()
    .describe("Email address to send the invoice to. Omit to use the invoice's BillEmail."),
  allow_resend: z
    .boolean()
    .optional()
    .default(false)
    .describe("Send even if the invoice was already emailed (EmailStatus=EmailSent)."),
});

const toolHandler = async ({ params }: any) => {
  const { invoice_id, send_to, allow_resend } = params;
  const response = await sendQuickbooksInvoice(invoice_id, send_to, allow_resend === true);

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
  const to = send_to ?? invoice.BillEmail?.Address ?? "the invoice's BillEmail address";
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
