import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  tutorialSidebar: [
    {
      type: 'category',
      label: 'Documentation',
      link: {
        type: 'generated-index',
        title: 'Documentation',
        description: 'Get started with Voxbento',
        slug: '/documentation',
      },
      items: [
        {
          type: 'category',
          label: 'Get Started',
          items: ['introduction', 'quickstart', 'how-it-works'],
        },
        {
          type: 'category',
          label: 'Admin Guide',
          items: [
            'admin/events-and-rooms',
            'admin/booths',
            'admin/members-and-roles',
            'admin/invite-tokens',
            'admin/transcription',
          ],
        },
        {
          type: 'category',
          label: 'Interpreter Guide',
          items: [
            'interpreter/joining-a-booth',
            'interpreter/going-live',
            'interpreter/handoff',
          ],
        },
        {
          type: 'category',
          label: 'Listener Guide',
          items: ['listener/attending-an-event', 'listener/live-captions'],
        },
        {
          type: 'category',
          label: 'Configuration',
          items: [
            'configuration/environment-variables',
            'configuration/deployment',
            'configuration/security',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: 'API Reference',
      link: {
        type: 'generated-index',
        title: 'API Reference',
        description: 'Voxbento API documentation',
        slug: '/api',
      },
      items: [
        {
          type: 'category',
          label: 'Overview',
          items: ['api/overview', 'api/authentication'],
        },
        {
          type: 'category',
          label: 'Booths',
          items: ['api/booths/state', 'api/booths/whip-whep', 'api/booths/events'],
        },
        {
          type: 'category',
          label: 'Real-Time',
          items: ['api/websocket/booth', 'api/websocket/captions'],
        },
      ],
    },
  ],
};

export default sidebars;
