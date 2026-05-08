import json
import os
from pathlib import Path
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough

# API 키 설정 (테스트용)
if not os.getenv('GOOGLE_API_KEY'):
    os.environ['GOOGLE_API_KEY'] = ""


def load_menu_data(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    docs = []

    def process_node(node, path=[]):
        name = node.get('name', '')
        url = node.get('url', '')
        children = node.get('children', [])

        current = path + [name]

        if url:
            path_text = ' > '.join(current)
            content = f"메뉴: {path_text}\nURL: {url}"

            docs.append(Document(
                page_content=content,
                metadata={'name': name, 'url': url, 'path': path_text}
            ))

        for child in children:
            process_node(child, current)

    for item in data:
        process_node(item)

    return docs


def setup_vectorstore(docs, db_path="./kt_faiss_db"):
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    index_file = Path(db_path) / "index.faiss"
    if index_file.exists():
        print(f"기존 DB 로드중...")
        return FAISS.load_local(db_path, embeddings, allow_dangerous_deserialization=True)

    print("벡터 DB 생성중... (시간이 좀 걸릴 수 있습니다)")
    vectorstore = FAISS.from_documents(docs, embeddings)
    vectorstore.save_local(db_path)
    return vectorstore


def main():
    print("=" * 50)
    print("KT 메뉴 챗봇")
    print("=" * 50)

    # 데이터 로드
    docs = load_menu_data("kt_menu_crawled.json")
    print(f"{len(docs)}개 메뉴 로드 완료")

    # 벡터스토어 설정
    vectorstore = setup_vectorstore(docs)

    # LLM & RAG 체인
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 10})

    prompt = ChatPromptTemplate.from_template("""다음 컨텍스트를 참고하여 질문에 답변하세요.

컨텍스트에 정확히 일치하는 메뉴가 없으면, 가장 유사한 메뉴를 찾아 안내해주세요.

컨텍스트: {context}

질문: {question}

답변:""")

    def format_docs(docs):
        return "\n\n".join(doc.page_content for doc in docs)

    qa_chain = (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    print("준비 완료!")
    print("-" * 50)

    # 챗봇 루프
    while True:
        query = input("\n질문: ").strip()

        if query.lower() in ['quit', 'exit', '종료']:
            break

        if not query:
            continue

        try:
            # 답변 생성
            answer = qa_chain.invoke(query)
            print(f"\n답변: {answer}\n")

            # 관련 메뉴 표시
            docs = retriever.invoke(query)
            if docs:
                print("관련 메뉴:")
                for i, doc in enumerate(docs[:3], 1):
                    print(f"  {i}. {doc.metadata['path']}")
                    print(f"     URL: {doc.metadata['url']}")
        except Exception as e:
            print(f"오류: {e}")

    print("\n종료합니다.")


if __name__ == "__main__":
    main()
