import { generateStaticParamsFor, importPage } from 'nextra/pages'

export const generateStaticParams =
  generateStaticParamsFor('mdxPath')

export async function generateMetadata(props: any) {
  const params = await props.params
  const { metadata } = await importPage(params.mdxPath)
  return metadata
}

export default async function Page(props: any) {
  const params = await props.params

  const result = await importPage(params.mdxPath)

  const {
    default: MDXContent
  } = result

  return <MDXContent {...props} params={params} />
}