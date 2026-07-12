FROM eclipse-temurin:21-jre

ARG AGENTSPAN_SERVER_URL=https://agentspan.s3.us-east-2.amazonaws.com/agentspan-server-latest.jar
ADD ${AGENTSPAN_SERVER_URL} /opt/agentspan/agentspan-runtime.jar

EXPOSE 6767
ENTRYPOINT ["java", "-jar", "/opt/agentspan/agentspan-runtime.jar"]
