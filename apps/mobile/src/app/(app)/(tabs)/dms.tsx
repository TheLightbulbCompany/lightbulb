import { RoomsScreen } from "./home";

/**
 * The DMs tab — Home's rows restricted to `dm`/`group-dm`, unsectioned and
 * newest first. Same component, one flag: the two tabs differ only in
 * sectioning.
 */
export default function Dms() {
  return <RoomsScreen direct />;
}
