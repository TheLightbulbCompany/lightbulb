/** A date is a schedule, never proof that the assistant joined or listened. */
export interface MeetingStatusInput {
  status?: string | null;
  startAt?: number | null;
  lastBotStatus?: string | null;
  inCallAt?: number | null;
  hasTranscript?: boolean;
}

export function meetingStatus(meeting: MeetingStatusInput, now = Date.now()) {
  const joined = meeting.status === "in_call" || !!meeting.inCallAt || !!meeting.hasTranscript;
  if (meeting.status === "cancelled") return { label: "Cancelled", detail: "Lightbulb will not join this occurrence.", joined };
  if (meeting.status === "bot_failed") return { label: joined ? "Connection ended" : "Couldn't join", detail: "Check the meeting link and host access.", joined };
  if (meeting.status === "ended") return { label: "Ended", detail: joined ? "Lightbulb joined this meeting." : "Join confirmation is unavailable for this meeting.", joined };
  if (meeting.status === "in_call") return { label: "Live", detail: "Lightbulb is in the meeting.", joined };
  if (meeting.status === "scheduled" || meeting.status === "bot_created") {
    if (meeting.lastBotStatus === "in_waiting_room") return { label: "Waiting for host", detail: "Let Lightbulb in from the meeting lobby.", joined: false };
    if (meeting.lastBotStatus === "joining_call") return { label: "Joining", detail: "Lightbulb is connecting to the meeting.", joined: false };
    if (meeting.startAt != null && meeting.startAt * 1000 > now) return { label: "Scheduled", detail: "Lightbulb is scheduled to join this meeting.", joined: false };
    return { label: "Join not confirmed", detail: "Waiting for confirmation that Lightbulb joined. Check the meeting link and host access.", joined: false };
  }
  return { label: "Status unavailable", detail: "We haven't confirmed whether Lightbulb joined this meeting.", joined };
}
