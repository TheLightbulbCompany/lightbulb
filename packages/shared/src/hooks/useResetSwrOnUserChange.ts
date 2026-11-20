import { useEffect, useRef } from "react";
import { mutate } from "swr";

/** Drop every cached SWR read when the signed-in person changes (sign-out, or another account on this app). Most keys
 *  carry no user (a task, a room's detail, `/billing/account`), and each holds what one viewer may see: without this
 *  the next person would render the last one's answers until each revalidated. Nothing to drop on the first sign-in. */
export function useResetSwrOnUserChange(userId: string | null | undefined): void {
  const last = useRef(userId);
  useEffect(() => {
    if (last.current && last.current !== userId) void mutate(() => true, undefined, { revalidate: false });
    last.current = userId;
  }, [userId]);
}
