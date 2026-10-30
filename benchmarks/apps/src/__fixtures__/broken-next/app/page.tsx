const n: number = "not a number"; // deliberate type error
export default function P() { return <p>{n}</p>; }
