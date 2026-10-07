import { QuickbooksClient } from "../clients/quickbooks-client.js";
import { ToolResponse } from "../types/tool-response.js";
import { formatError } from "../helpers/format-error.js";

/**
 * Email an invoice from QuickBooks Online to the customer.
 *
 * Wraps node-quickbooks's `sendInvoicePdf`, which POSTs
 * `/v3/company/{realmId}/invoice/{id}/send[?sendTo=<email>]`. QuickBooks sends
 * its own invoice email (PDF attached, Pay link when payments are on) and
 * answers with the invoice, now carrying `EmailStatus: "EmailSent"`.
 *
 * Two guards, same as upstream PR intuit/quickbooks-online-mcp-server#151
 * (open, unreviewed since 2026-09-30): QuickBooks does not deduplicate sends,
 * so an invoice already marked EmailSent is refused unless allowResend, and
 * an invoice with no BillEmail and no sendTo is refused before the call.
 */
function callQbo(quickbooks: any, method: string, ...args: unknown[]): Promise<any> {
  return new Promise((resolve, reject) => {
    quickbooks[method](...args, (err: any, data: any) => (err ? reject(err) : resolve(data)));
  });
}

export async function sendQuickbooksInvoice(
  invoiceId: string,
  sendTo?: string,
  allowResend = false
): Promise<ToolResponse<any>> {
  try {
    const quickbooks = await QuickbooksClient.getInstance();
    const current = await callQbo(quickbooks, "getInvoice", invoiceId);

    if (!allowResend && current?.EmailStatus === "EmailSent") {
      return {
        result: null,
        isError: true,
        error:
          `Invoice ${invoiceId} was already emailed (EmailStatus=EmailSent). QuickBooks does not ` +
          `deduplicate sends, so it was NOT sent again. Pass allow_resend: true only if the user ` +
          `explicitly asked to send it again.`,
      };
    }
    if (!sendTo && !current?.BillEmail?.Address) {
      return {
        result: null,
        isError: true,
        error:
          `Invoice ${invoiceId} has no BillEmail address, so there is no recipient. Nothing was ` +
          `sent. Pass send_to with the address the user provided.`,
      };
    }

    // node-quickbooks appends sendTo to the URL verbatim, so encode it here.
    const sent = await callQbo(
      quickbooks,
      "sendInvoicePdf",
      invoiceId,
      sendTo ? encodeURIComponent(sendTo) : undefined
    );
    return { result: sent, isError: false, error: null };
  } catch (error) {
    return { result: null, isError: true, error: formatError(error) };
  }
}
