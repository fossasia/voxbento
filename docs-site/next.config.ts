import nextra from 'nextra'

const withNextra = nextra({
  contentDirBasePath: '/content'
})

export default withNextra({
  reactStrictMode: true
})
