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
    expect(HOME_TITLE).toBe('AI Receptionist for Salons, Spas & Barbershops | Lightbulb');
    expect(HOME_TITLE.length).toBeLessThanOrEqual(60);
    expect(HOME_DESCRIPTION).toBe(
      "Your salon's AI receptionist answers your calls day and night, books into Google Calendar, takes messages and hands callers to you. Keep your number.",
    );
    expect(HOME_DESCRIPTION.length).toBeLessThanOrEqual(160);
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
      applicationSubCategory: 'AI receptionist',
    });
    // No invented ratings or reviews anywhere in the entity graph.
    expect(JSON.stringify(homeJsonLd)).not.toMatch(/aggregateRating|"review"/i);
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
