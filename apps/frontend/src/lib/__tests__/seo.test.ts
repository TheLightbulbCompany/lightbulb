import { describe, expect, it } from 'vitest';

import {
  HOME_DESCRIPTION,
  HOME_TITLE,
  organizationJsonLd,
  softwareApplicationJsonLd,
  websiteJsonLd,
} from '@/lib/seo';

describe('homepage search metadata', () => {
  it('uses the exact brand and the product category people can recognize', () => {
    expect(HOME_TITLE).toBe('Lightbulb AI — Shared Workspace for Humans and AI Agents');
    expect(HOME_DESCRIPTION).toBe(
      'Lightbulb is a shared workspace where people and AI agents work together in channels, with persistent context, tasks, approvals, and connected tools.',
    );
  });
});

describe('homepage entity graph', () => {
  it('connects the legal company, public brand, website, and product with stable IDs', () => {
    expect(organizationJsonLd).toMatchObject({
      '@type': 'Organization',
      '@id': 'https://thelightbulb.company/#organization',
      name: 'Lightbulb AI',
      legalName: 'Isol8 Inc.',
      alternateName: ['Lightbulb', 'The Lightbulb Company'],
      url: 'https://thelightbulb.company',
      sameAs: expect.arrayContaining([
        'https://x.com/lightbulbai',
        'https://www.linkedin.com/company/isol8ai/',
      ]),
    });

    expect(websiteJsonLd).toMatchObject({
      '@id': 'https://thelightbulb.company/#website',
      publisher: { '@id': 'https://thelightbulb.company/#organization' },
    });

    expect(softwareApplicationJsonLd).toMatchObject({
      '@id': 'https://thelightbulb.company/#software',
      brand: { '@id': 'https://thelightbulb.company/#organization' },
      provider: { '@id': 'https://thelightbulb.company/#organization' },
    });
  });
});
