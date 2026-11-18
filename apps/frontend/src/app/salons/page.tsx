import { VerticalPage, verticalMetadata } from "@/components/vertical/VerticalPage";
import { getVertical } from "@/components/vertical/verticals";

const v = getVertical("salons");

export const metadata = verticalMetadata(v);

export default function Page() {
  return <VerticalPage v={v} />;
}
