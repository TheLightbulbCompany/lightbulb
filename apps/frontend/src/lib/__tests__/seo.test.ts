import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  HOME_DESCRIPTION,
  HOME_TITLE,
  PHONE_DESCRIPTION,
  PHONE_TITLE,
  SITE_DESCRIPTION,
  SITE_TITLE,
  hubJsonLd,
  organizationJsonLd,
  phoneJsonLd,
  softwareApplicationJsonLd,
  websiteJsonLd,
} from '@/lib/seo';
import { HOME_FAQ } from '@/lib/homeFaq';

describe('search metadata', () => {
  it('gives /phone the receptionist title people can recognize', () => {
    expect(PHONE_TITLE).toBe('AI Receptionist for Salons, Spas & Barbershops | Lightbulb');
    expect(PHONE_TITLE.length).toBeLessThanOrEqual(60);
    expect(PHONE_DESCRIPTION).toBe(
      "Your salon's AI receptionist answers your calls day and night, books into Google Calendar, takes messages and hands callers to you. Keep your number.",
    );
    expect(PHONE_DESCRIPTION.length).toBeLessThanOrEqual(160);
  });

  it('gives / the company title and names the three products', () => {
    expect(HOME_TITLE).toBe('The Voice AI Application Company | Lightbulb');
    expect(HOME_TITLE.length).toBeLessThanOrEqual(60);
    expect(HOME_DESCRIPTION).toBe(
      'Lightbulb Phone answers your calls and books appointments. Lightbulb Meetings takes notes in Google Meet. Spark is a voice app for the Mac.',
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
      '@id': 'https://thelightbulb.company/phone#software',
      name: 'Lightbulb Phone',
      url: 'https://thelightbulb.company/phone',
      brand: { '@id': 'https://thelightbulb.company/#organization' },
      provider: { '@id': 'https://thelightbulb.company/#organization' },
      applicationSubCategory: 'AI receptionist',
    });
    // No invented ratings or reviews anywhere in the entity graph.
    expect(JSON.stringify([phoneJsonLd, hubJsonLd])).not.toMatch(/aggregateRating|"review"/i);
  });
});

describe('/phone @graph', () => {
  it('carries the visible FAQ word for word and no price before /pricing', () => {
    const graph = phoneJsonLd['@graph'] as Record<string, unknown>[];
    const faq = graph.find((n) => n['@type'] === 'FAQPage') as { '@id': string; mainEntity: { name: string; acceptedAnswer: { text: string } }[] };
    expect(faq['@id']).toBe('https://thelightbulb.company/phone#faq');
    expect(graph.find((n) => n['@type'] === 'WebPage')?.['@id']).toBe('https://thelightbulb.company/phone#webpage');
    expect(faq.mainEntity.map((q) => [q.name, q.acceptedAnswer.text])).toEqual(HOME_FAQ.map(({ q, a }) => [q, a]));
    expect(JSON.stringify(phoneJsonLd)).not.toMatch(/"offers"|"price"/);
    expect(graph.every((n) => !('@context' in n))).toBe(true);
  });
});

describe('/ @graph', () => {
  it('is the organization, the website and a page about the organization: no FAQ, product or offers', () => {
    const graph = hubJsonLd['@graph'] as Record<string, unknown>[];
    expect(graph.map((n) => n['@type'])).toEqual(['Organization', 'WebSite', 'WebPage']);
    expect(graph[2]).toMatchObject({ about: { '@id': 'https://thelightbulb.company/#organization' } });
    expect(JSON.stringify(hubJsonLd)).not.toMatch(/FAQPage|SoftwareApplication|"offers"/);
    // The founder's H1 is positioning, not a claim for structured data.
    expect(JSON.stringify(hubJsonLd)).not.toMatch(/number one/i);
    expect(graph.every((n) => !('@context' in n))).toBe(true);
  });
});

describe('root layout default', () => {
  it('is neutral: not the home headline and not the receptionist', () => {
    expect(SITE_TITLE).toBe('Lightbulb');
    expect(SITE_DESCRIPTION).toBe(
      'Lightbulb makes voice assistants: Lightbulb Phone, Lightbulb Meetings and Spark.',
    );
    for (const s of [SITE_TITLE, SITE_DESCRIPTION]) {
      expect(s).not.toMatch(/number one/i);
      expect(s).not.toMatch(/salon|receptionist/i);
    }
    const layout = readFileSync(join(__dirname, '../../app/layout.tsx'), 'utf8');
    expect(layout).toMatch(/import \{ OG_IMAGE, SITE_DESCRIPTION, SITE_TITLE \} from "@\/lib\/seo";/);
    expect(layout).not.toMatch(/HOME_TITLE|HOME_DESCRIPTION/);
  });
});
