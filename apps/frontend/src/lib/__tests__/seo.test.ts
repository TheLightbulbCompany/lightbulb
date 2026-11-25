import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { SITE_DESCRIPTION, SITE_TITLE } from '@/lib/seo';

describe('root layout default', () => {
  it('names Spark and none of the retired products', () => {
    expect(SITE_TITLE).toBe('Lightbulb');
    expect(SITE_DESCRIPTION).toBe('Lightbulb makes Spark, a one-click voice interface for your Mac.');
    for (const s of [SITE_TITLE, SITE_DESCRIPTION]) {
      expect(s).not.toMatch(/salon|receptionist|Meetings|Phone/i);
    }
    const layout = readFileSync(join(__dirname, '../../app/layout.tsx'), 'utf8');
    expect(layout).toMatch(/import \{ OG_IMAGE, SITE_DESCRIPTION, SITE_TITLE \} from "@\/lib\/seo";/);
  });
});
