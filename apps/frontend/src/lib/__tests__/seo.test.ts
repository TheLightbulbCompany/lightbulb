import { describe, expect, it } from 'vitest';

import {
  HOME_DESCRIPTION,
  HOME_TITLE,
  homeJsonLd,
  organizationJsonLd,
  softwareApplicationJsonLd,
  websiteJsonLd,
} from '@/lib/seo';
import { HOME_FAQ } from '@/lib/homeFaq';

describe('homepage search metadata', () => {
  it('uses the exact brand and the product category people can recognize', () => {
    expect(HOME_TITLE).toBe('Lightbulb — Messaging for people and their AI agents');
    expect(HOME_DESCRIPTION).toBe(
      "Your coding agents talk to each other and to your teammates' agents, free and end-to-end encrypted. Add a workspace and your agents keep working when you log off.",
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
      sameAs: [
        'https://x.com/lightbulbai',
        'https://github.com/TheLightbulbCompany',
        'https://www.producthunt.com/products/lightbulb-2',
      ],
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

describe('homepage @graph', () => {
  it('carries the visible FAQ word for word and no price before /pricing', () => {
    const graph = homeJsonLd['@graph'] as Record<string, unknown>[];
    const faq = graph.find((n) => n['@type'] === 'FAQPage') as { mainEntity: { name: string; acceptedAnswer: { text: string } }[] };
    expect(faq.mainEntity.map((q) => [q.name, q.acceptedAnswer.text])).toEqual(HOME_FAQ.map(({ q, a }) => [q, a]));
    expect(JSON.stringify(homeJsonLd)).not.toMatch(/"offers"|"price"/);
    expect(graph.every((n) => !('@context' in n))).toBe(true);
  });
});
