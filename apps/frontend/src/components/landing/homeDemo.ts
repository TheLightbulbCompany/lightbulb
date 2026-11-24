// The working window's examples on / (HomeLanding draws them, HomePlayer plays them). Every name is an example,
// and the window says so. Facts follow .agents/product-marketing.md (Phone receptionist, Google Meet agent, Spark).

type Row = [time: string, written: string];
export type Call = {
  who: string;
  time: string;
  day: string;
  len: string;
  chip: [kind: "booked" | "needs" | "moved", label: string];
  sum: string;
  lines: [who: string, said: string][];
  did: string[];
  book: { day: string; chair: string; rows: Row[]; at?: string; text?: string; sticky?: boolean };
};

const SAT: Row[] = [
  ["9:00", "Mia, color"],
  ["9:30", ""],
  ["10:00", ""],
  ["10:30", ""],
  ["11:00", "Ray, beard trim"],
  ["11:30", ""],
  ["12:00", "Tess, blowout"],
  ["12:30", ""],
  ["1:00", "Lunch"],
];
const HELLO = "Hi, I'm Nova, the salon's AI assistant. How can I help?";

export const CALLS: Call[] = [
  {
    who: "Dana",
    time: "7:12 pm",
    day: "Tuesday",
    len: "1:04",
    chip: ["booked", "Booked"],
    sum: "Haircut with Sam, Saturday 10:00.",
    lines: [
      ["Nova", HELLO],
      ["Dana", "Do you have anything Saturday morning for a haircut?"],
      ["Nova", "Sam has 10:00 or 11:30. Which works for you?"],
      ["Dana", "10, please."],
      ["Nova", "You're booked with Sam, Saturday at 10."],
    ],
    did: ["Checked Saturday morning with Sam", "Booked Dana for a haircut, Saturday 10:00"],
    book: { day: "Saturday", chair: "Sam's chair", rows: SAT, at: "10:00", text: "Dana, haircut" },
  },
  {
    who: "Lena",
    time: "6:40 pm",
    day: "Tuesday",
    len: "0:52",
    chip: ["needs", "Needs you"],
    sum: "Bridal party of six. Took a message.",
    lines: [
      ["Nova", HELLO],
      ["Lena", "Can you do hair for a bridal party of six in June?"],
      ["Nova", "I'll take the details and have the owner call you back."],
      ["Lena", "June 14th, six people, in the morning."],
    ],
    did: ["Took a message: bridal party, six people, June 14", "Put it in Needs you for the owner"],
    book: { day: "Saturday", chair: "Sam's chair", rows: SAT, sticky: true },
  },
  {
    who: "Rosa",
    time: "2:15 pm",
    day: "Tuesday",
    len: "0:41",
    chip: ["moved", "Moved"],
    sum: "Color moved to Thursday 2:00 with Jo.",
    lines: [
      ["Nova", HELLO],
      ["Rosa", "I need to move my color on Wednesday."],
      ["Nova", "Thursday at 2:00 is open with Jo. Shall I move it?"],
      ["Rosa", "Yes, thanks."],
    ],
    did: ["Found Rosa's Wednesday booking", "Moved it to Thursday 2:00 with Jo"],
    book: {
      day: "Thursday",
      chair: "Jo's chair",
      rows: [
        ["12:00", "Ava, cut"],
        ["12:30", ""],
        ["1:00", "Lunch"],
        ["1:30", ""],
        ["2:00", ""],
        ["2:30", ""],
        ["3:00", "Kim, gloss"],
        ["3:30", ""],
        ["4:00", ""],
      ],
      at: "2:00",
      text: "Rosa, color",
    },
  },
];

export const STICKY = "Call Lena back: bridal party of six, June 14, morning.";

// [tile, name, what they said, the note it leaves (list id, text)]
export const STEPS: [string, string, string, [string, string] | null][] = [
  ["priya", "Priya", "Saturday walk-ins are up since the new sign.", ["nNotes", "Saturday walk-ins are up since the new sign."]],
  ["dan", "Dan", "Then let's open at nine on Saturdays.", null],
  ["mei", "Mei", "I'll take the first chair at nine.", ["nDec", "Open at 9 on Saturdays. Mei takes the first chair."]],
  ["jo", "Jo", "Nova, what did we decide? I'll update the hours online.", null],
  [
    "nova",
    "Nova",
    "Open at nine on Saturdays, and Mei takes the first chair. I added a follow-up for you, Jo.",
    ["nFol", "Jo updates the Saturday hours online."],
  ],
];

export const TILES: [id: string, name: string, color: string][] = [
  ["priya", "Priya", "var(--pri)"],
  ["dan", "Dan", "var(--dan)"],
  ["mei", "Mei", "var(--mei)"],
  ["jo", "Jo", "var(--jo)"],
  ["sam", "Sam", "var(--sam)"],
];

export const ASKS: [key: "photos" | "folder", said: string][] = [
  ["photos", "Text Sam the beach photos."],
  ["folder", "Put these PDFs in a folder called Receipts."],
];
