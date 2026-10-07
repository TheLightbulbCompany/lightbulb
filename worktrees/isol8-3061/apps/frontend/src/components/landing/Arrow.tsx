/** The thin CTA arrow. Deliberately lighter than a "→" glyph. */
export function Arrow() {
  return (
    <svg className="lb-arw" width="18" height="12" viewBox="0 0 18 12" fill="none" aria-hidden="true">
      <path
        d="M1 6h15.2M11.6 1.1 16.9 6l-5.3 4.9"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default Arrow;
