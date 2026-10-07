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
 */
export async function sendQuickbooksInvoice(
  invoiceId: string,
  sendTo?: string
): Promise<ToolResponse<any>> {
  try {
    const quickbooks = await QuickbooksClient.getInstance();

    return new Promise((resolve) => {
      const done = (err: any, invoice: any) => {
        if (err) {
          resolve({ result: null, isError: true, error: formatError(err) });
        } else {
          resolve({ result: invoice, isError: false, error: null });
        }
      };
      // node-quickbooks appends sendTo to the URL verbatim, so encode it here.
      if (sendTo) {
        (quickbooks as any).sendInvoicePdf(invoiceId, encodeURIComponent(sendTo), done);
      } else {
        (quickbooks as any).sendInvoicePdf(invoiceId, done);
      }
    });
  } catch (error) {
    return { result: null, isError: true, error: formatError(error) };
  }
}
